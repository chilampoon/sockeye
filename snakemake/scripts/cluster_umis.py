import argparse
import itertools
import logging
import multiprocessing
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time

import dask.dataframe as dd
import numpy as np
import pandas as pd
import pysam
from dask.distributed import Client
from editdistance import eval as edit_distance
from tqdm import tqdm

logger = logging.getLogger(__name__)


def parse_args():
    # Create argument parser
    parser = argparse.ArgumentParser()

    # Positional mandatory arguments
    parser.add_argument(
        "bam",
        help="BAM file of alignments with tags for gene (GN), corrected barcode \
        (CB) and uncorrected UMI (UY)",
        type=str,
    )

    # Optional arguments
    parser.add_argument(
        "--output",
        help="Output BAM file with new tags for corrected UMI (UB) \
        [tagged.sorted.bam]",
        type=str,
        default="tagged.sorted.bam",
    )

    parser.add_argument(
        "-i",
        "--ref_interval",
        help="Size of genomic window (bp) to assign as gene name if no gene \
        assigned by featureCounts [1000]",
        type=int,
        default=1000,
    )

    parser.add_argument(
        "-t", "--threads", help="Threads to use [4]", type=int, default=4
    )

    parser.add_argument(
        "--verbosity",
        help="logging level: <=2 logs info, <=3 logs warnings",
        type=int,
        default=2,
    )

    # Parse arguments
    args = parser.parse_args()

    # Create temp dir and add that to the args object
    p = pathlib.Path(args.output)
    tempdir = tempfile.TemporaryDirectory(prefix="tmp.", dir=p.parents[0])
    args.tempdir = tempdir.name

    return args


def init_logger(args):
    logging.basicConfig(
        format="%(asctime)s -- %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    logging_level = args.verbosity * 10
    logging.root.setLevel(logging_level)
    logging.root.handlers[0].addFilter(lambda x: "NumExpr" not in x.msg)


def breadth_first_search(node, adj_list):
    searched = set()
    queue = set()
    queue.update((node,))
    searched.update((node,))

    while len(queue) > 0:
        node = queue.pop()
        for next_node in adj_list[node]:
            if next_node not in searched:
                queue.update((next_node,))
                searched.update((next_node,))

    return searched


def get_adj_list_directional(umis, counts, threshold=2):
    """
    identify all umis within the LEVENSHTEIN distance threshold
    and where the counts of the first umi is > (2 * second umi counts)-1
    """

    adj_list = {umi: [] for umi in umis}
    iter_umi_pairs = itertools.combinations(umis, 2)
    for umi1, umi2 in iter_umi_pairs:
        if edit_distance(umi1, umi2) <= threshold:
            if counts[umi1] >= (counts[umi2] * 2) - 1:
                adj_list[umi1].append(umi2)
            if counts[umi2] >= (counts[umi1] * 2) - 1:
                adj_list[umi2].append(umi1)

    return adj_list


def get_connected_components_adjacency(umis, graph, counts):
    """
    find the connected UMIs within an adjacency dictionary
    """

    # TS: TO DO: Work out why recursive function doesn't lead to same
    # final output. Then uncomment below

    # if len(graph) < 10000:
    #    self.search = breadth_first_search_recursive
    # else:
    #    self.search = breadth_first_search

    found = set()
    components = list()

    for node in sorted(graph, key=lambda x: counts[x], reverse=True):
        if node not in found:
            # component = self.search(node, graph)
            component = breadth_first_search(node, graph)
            found.update(component)
            components.append(component)
    return components


def group_directional(clusters, adj_list, counts):
    """
    return groups for directional method
    """

    observed = set()
    groups = []
    for cluster in clusters:
        if len(cluster) == 1:
            groups.append(list(cluster))
            observed.update(cluster)
        else:
            cluster = sorted(cluster, key=lambda x: counts[x], reverse=True)
            # need to remove any node which has already been observed
            temp_cluster = []
            for node in cluster:
                if node not in observed:
                    temp_cluster.append(node)
                    observed.add(node)
            groups.append(temp_cluster)

    return groups


def cluster(counts_dict, threshold=3):
    adj_list = get_adj_list_directional(counts_dict.keys(), counts_dict, threshold)
    clusters = get_connected_components_adjacency(
        counts_dict.keys(), adj_list, counts_dict
    )
    final_umis = [list(x) for x in group_directional(clusters, adj_list, counts_dict)]
    return final_umis


def create_map_to_correct_umi(cluster_list):
    my_map = {y: x[0] for x in cluster_list for y in x}
    return my_map


def correct_umis(umis):
    # logging.info(f"clustering {len(umis)} UMIs")
    counts_dict = dict(umis.value_counts())
    umi_map = create_map_to_correct_umi(cluster(counts_dict))
    # for k,v in umi_map.items():
    #     if k!=v:
    #         print(f"{k} --> {v}")
    #     else:
    #         print("no correction")
    return umis.replace(umi_map)


def add_tags(chrom, umis, genes, args):
    """
    Using the read_id:umi_corr and read_id:gene dictionaries, add UB:Z and GN:Z
    tags to the output BAM file.
    """
    bam_out_fn = args.output

    bam = pysam.AlignmentFile(args.bam, "rb")
    bam_out = pysam.AlignmentFile(bam_out_fn, "wb", template=bam)

    for align in bam.fetch(chrom):
        read_id = align.query_name

        # Corrected UMI = UB:Z
        align.set_tag("UB", umis[read_id], value_type="Z")

        # Annotated gene name = GN:Z
        align.set_tag("GN", genes[read_id], value_type="Z")

        bam_out.write(align)

    bam.close()
    bam_out.close()


def get_bam_info(bam):
    """
    Use `samtools idxstat` to get number of alignments and names of all contigs
    in the reference.

    :param bam: Path to sorted BAM file
    :type bame: str
    :return: Sum of all alignments in the BAM index file and list of all chroms
    :rtype: int,list
    """
    bam = pysam.AlignmentFile(bam, "rb")
    stats = bam.get_index_statistics()
    n_aligns = int(sum([contig.mapped for contig in stats]))
    chroms = dict(
        [(contig.contig, contig.mapped) for contig in stats if contig.mapped > 0]
    )
    bam.close()
    return n_aligns, chroms


def create_region_name(align, args):
    """
    Create a 'gene name' based on the aligned chromosome and coordinates.
    The midpoint of the alignment determines which genomic interval to use
    for the 'gene name'.

    :param align: pysam BAM alignment
    :type align: class 'pysam.libcalignedsegment.AlignedSegment'
    :param args: object containing all supplied arguments
    :type args: class 'argparse.Namespace'
    :return: Newly created 'gene name' based on aligned chromosome and coords
    :rtype: str
    """
    chrom = align.reference_name
    start_pos = align.get_reference_positions()[0]
    end_pos = align.get_reference_positions()[-1]

    # Find the midpoint of the alignment
    midpoint = int((start_pos + end_pos) / 2)

    # Pick the genomic interval based on this alignment midpoint
    interval_start = np.floor(midpoint / args.ref_interval) * args.ref_interval
    interval_end = np.ceil(midpoint / args.ref_interval) * args.ref_interval

    # New 'gene name' will be <chr>_<interval_start>_<interval_end>
    gene = f"{chrom}_{int(interval_start)}_{int(interval_end)}"
    return gene


def get_num_partitions(args):
    """
    Determine how many partitions to create for the Dask dataframe. From Dask's
    best practices page:

    "it’s common for Dask to have 2-3 times as many chunks a if you have 1 GB
    chunks and ten cores, then Dask is likely to use at least 10 GB of memory.
    Additionally, it’s common for Dask to have 2-3 times as many chunks
    available to work on so that it always has something to work on."

    So we will set npartitions to be several times the number of threads
    specified in the command line args
    """
    return 3 * args.threads


def process_bam_records(tup):
    """
    Read through all the alignments for specific chromosome to pull out
    the gene, barcode, and uncorrected UMI information. Use that to cluster
    UMIs and get the corrected UMI sequence. We'll then write out a temporary
    chromosome-specific BAM file with the corrected UMI tag (UB).

    :param tup: Tuple containing the input arguments
    :type tup: tup
    :return: Path to a temporary BAM file
    :rtype: str
    """
    input_bam = tup[0]
    chrom = tup[1]
    args = tup[2]

    # Open input BAM file
    bam = pysam.AlignmentFile(input_bam, "rb")

    records = []
    for align in bam.fetch(contig=chrom):
        read_id = align.query_name

        # Make sure each alignment in this BAM has the expected tags
        for tag in ["UR", "CB", "GN"]:
            assert align.has_tag(tag), f"{tag} tag not found in f{read_id}"

        # Corrected cell barcode = CB:Z
        bc_corr = align.get_tag("CB")

        # Uncorrected UMI = UR:Z
        umi_uncorr = align.get_tag("UR")

        # Annotated gene name = GN:Z
        gene = align.get_tag("GN")

        # If no gene annotation exists
        if gene == "NA":
            # Group by region if no gene annotation
            gene = create_region_name(align, args)

        records.append((read_id, gene, bc_corr, umi_uncorr))

    # Done reading the chrom-specific alignments from the BAM
    bam.close()

    # Create a dataframe with chrom-specific data
    df = pd.DataFrame.from_records(
        records, columns=["read_id", "gene", "bc", "umi_uncorr"]
    )

    # This was the original vanilla pandas implementation
    # start = time.time()
    # print(df.shape)
    # print(df.head(10))
    # df["umi_corr"] = df.groupby(["gene", "bc"])["umi_uncorr"].apply(correct_umis)
    # end = time.time()
    # print(df.head(10))
    # print("PANDAS", end-start)
    # df = df.drop("umi_corr", axis=1)

    ############################################################################
    # Dask implementation. Split df into partitions for parallel groupby-apply #
    ############################################################################
    # client = Client(n_workers=args.threads, threads_per_worker=1)
    Client(n_workers=args.threads, threads_per_worker=1)

    # Index on gene to get clean partition boundaries (no genes split across
    # partitions)
    df = df.sort_values("gene").set_index("gene")
    npartitions = get_num_partitions(args)
    ddf = dd.from_pandas(df, npartitions=npartitions, sort=True)

    # Need to reindex because indexing by gene is redundant (many repeats),
    # which otherwise causes the corrected umi results to be out of order
    ddf = ddf.reset_index(drop=False).reset_index(drop=False)
    ddf["new_index"] = ddf["gene"] + "_" + ddf["index"].astype("str")
    ddf = ddf.set_index("new_index")
    # start = time.time()
    ddf["umi_corr"] = (
        ddf.groupby(["gene", "bc"])["umi_uncorr"]
        .apply(correct_umis, meta=("umi_corr", "str"))
        .compute()
    )

    # Return to the pandas dataframe
    df = ddf.compute()
    # end = time.time()
    # print(end-start)

    # Simplify to a read_id:umi_corr dictionary
    df = df.drop(["index", "bc", "umi_uncorr"], axis=1).set_index("read_id")

    # Dict of corrected UMI for each read ID
    umis = df.to_dict()["umi_corr"]

    # Dict of gene names to add <chr>_<start>_<end> in place of NA
    genes = df.to_dict()["gene"]

    # Add corrected UMIs to each chrom-specific BAM entry via the UB:Z tag
    add_tags(chrom, umis, genes, args)


def main(args):
    init_logger(args)
    n_aligns, chroms = get_bam_info(args.bam)

    # Hopefully the chromosome name is prefix of BAM filename
    REGEX = r"([A-Za-z0-9.]+).bc_assign.gene.bam"
    m = re.search(REGEX, args.bam)
    assert m.group() is not None, "Unexpected input file naming convention!"

    chrom = m.group(1)
    func_args = (args.bam, chrom, args)
    process_bam_records(func_args)


if __name__ == "__main__":
    args = parse_args()

    main(args)
