"""PureCN: Copy number calling and SNV classification using targeted short read sequencing

https://github.com/lima1/PureCN
"""
import os
import shutil

import pandas as pd
import toolz as tz

from bcbio import utils
from bcbio.heterogeneity import chromhacks
from bcbio.log import logger
from bcbio.pipeline import datadict as dd
from bcbio.distributed.transaction import file_transaction
from bcbio.provenance import do
from bcbio.variation import germline, vcfutils
from bcbio.structural import cnvkit, gatkcnv

def run(items):
    paired = vcfutils.get_paired(items)
    if not paired:
        logger.info("Skipping PureCN; no somatic tumor calls in batch: %s" %
                    " ".join([dd.get_sample_name(d) for d in items]))
        return items
    work_dir = _sv_workdir(paired.tumor_data)
    purecn_out = _run_purecn(paired, work_dir)
    # XXX Currently finding edge case failures with Dx calling, needs additional testing
    # purecn_out = _run_purecn_dx(purecn_out, paired)
    purecn_out["variantcaller"] = "purecn"
    out = []
    if paired.normal_data:
        out.append(paired.normal_data)
    if "sv" not in paired.tumor_data:
        paired.tumor_data["sv"] = []
    paired.tumor_data["sv"].append(purecn_out)
    return out

def _run_purecn_dx(out, paired):
    """Extract signatures and mutational burdens from PureCN rds file.
    """
    out_base, out, all_files = _get_purecn_dx_files(paired, out)
    if not utils.file_uptodate(out["mutation_burden"], out["rds"]):
        with file_transaction(paired.tumor_data, out_base) as tx_out_base:
            cmd = ["PureCN_Dx.R", "--rds", out["rds"], "--callable", dd.get_sample_callable(paired.tumor_data),
                   "--signatures", "--out", tx_out_base]
            do.run(cmd, "PureCN Dx mutational burden and signatures")
            for f in all_files:
                if os.path.exists(os.path.join(os.path.dirname(tx_out_base), f)):
                    shutil.move(os.path.join(os.path.dirname(tx_out_base), f),
                                os.path.join(os.path.dirname(out_base), f))
    return out

def _get_purecn_dx_files(paired, out):
    """Retrieve files generated by PureCN_Dx
    """
    out_base = "%s-dx" % utils.splitext_plus(out["rds"])[0]
    all_files = []
    for key, ext in [[("mutation_burden",), "_mutation_burden.csv"],
                     [("plot", "signatures"), "_signatures.pdf"],
                     [("signatures",), "_signatures.csv"]]:
        cur_file = "%s%s" % (out_base, ext)
        out = tz.update_in(out, key, lambda x: cur_file)
        all_files.append(os.path.basename(cur_file))
    return out_base, out, all_files

def _run_purecn(paired, work_dir):
    """Run PureCN.R wrapper with pre-segmented CNVkit or GATK4 inputs.
    """
    segfns = {"cnvkit": _segment_normalized_cnvkit, "gatk-cnv": _segment_normalized_gatk}
    out_base, out, all_files = _get_purecn_files(paired, work_dir)
    cnr_file = tz.get_in(["depth", "bins", "normalized"], paired.tumor_data)
    if not utils.file_uptodate(out["rds"], cnr_file):
        cnr_file, seg_file = segfns[cnvkit.bin_approach(paired.tumor_data)](cnr_file, work_dir, paired)
        from bcbio import heterogeneity
        vcf_file = heterogeneity.get_variants(paired.tumor_data, include_germline=False)[0]["vrn_file"]
        vcf_file = germline.filter_to_pass_and_reject(vcf_file, paired, out_dir=work_dir)
        with file_transaction(paired.tumor_data, out_base) as tx_out_base:
            # Use UCSC style naming for human builds to support BSgenome
            genome = ("hg19" if dd.get_genome_build(paired.tumor_data) in ["GRCh37", "hg19"]
                      else dd.get_genome_build(paired.tumor_data))
            cmd = ["PureCN.R", "--seed", "42", "--out", tx_out_base, "--rds", "%s.rds" % tx_out_base,
                   "--sampleid", dd.get_sample_name(paired.tumor_data),
                   "--genome", genome,
                   "--vcf", vcf_file, "--tumor", cnr_file,
                   "--segfile", seg_file, "--funsegmentation", "Hclust", "--maxnonclonal", "0.3"]
            if dd.get_num_cores(paired.tumor_data) > 1:
                cmd += ["--cores", str(dd.get_num_cores(paired.tumor_data))]
            do.run(cmd, "PureCN copy number calling")
            for f in all_files:
                shutil.move(os.path.join(os.path.dirname(tx_out_base), f),
                            os.path.join(os.path.dirname(out_base), f))
    return out

def _segment_normalized_gatk(cnr_file, work_dir, paired):
    """Segmentation of normalized inputs using GATK4, converting into standard input formats.
    """
    work_dir = utils.safe_makedir(os.path.join(work_dir, "gatk-cnv"))
    seg_file = gatkcnv.model_segments(cnr_file, work_dir, paired)
    std_seg_file = seg_file.replace(".cr.seg", ".seg")
    if not utils.file_uptodate(std_seg_file, seg_file):
        with file_transaction(std_seg_file) as tx_out_file:
            df = pd.read_csv(seg_file, sep="\t", comment="@", header=0,
                             names=["chrom", "loc.start", "loc.end", "num.mark", "seg.mean"])
            df.insert(0, "ID", [dd.get_sample_name(paired.tumor_data)] * len(df))
            df.to_csv(tx_out_file, sep="\t", header=True, index=False)
    std_cnr_file = os.path.join(work_dir, "%s.cnr" % dd.get_sample_name(paired.tumor_data))
    if not utils.file_uptodate(std_cnr_file, cnr_file):
        with file_transaction(std_cnr_file) as tx_out_file:
            logdf = pd.read_csv(cnr_file, sep="\t", comment="@", header=0,
                                names=["chrom", "start", "end", "log2"])
            covdf = pd.read_csv(tz.get_in(["depth", "bins", "antitarget"], paired.tumor_data),
                                sep="\t", header=None,
                                names=["chrom", "start", "end", "orig.name", "depth", "gene"])
            df = pd.merge(logdf, covdf, on=["chrom", "start", "end"])
            del df["orig.name"]
            df = df[["chrom", "start", "end", "gene", "log2", "depth"]]
            df.insert(6, "weight", [1.0] * len(df))
            df.to_csv(tx_out_file, sep="\t", header=True, index=False)
    return std_cnr_file, std_seg_file

def _segment_normalized_cnvkit(cnr_file, work_dir, paired):
    """Segmentation of normalized inputs using CNVkit.
    """
    cnvkit_base = os.path.join(utils.safe_makedir(os.path.join(work_dir, "cnvkit")),
                                dd.get_sample_name(paired.tumor_data))
    cnr_file = chromhacks.bed_to_standardonly(cnr_file, paired.tumor_data, headers="chromosome",
                                                include_sex_chroms=True,
                                                out_dir=os.path.dirname(cnvkit_base))
    cnr_file = _remove_overlaps(cnr_file, os.path.dirname(cnvkit_base), paired.tumor_data)
    seg_file = cnvkit.segment_from_cnr(cnr_file, paired.tumor_data, cnvkit_base)
    return cnr_file, seg_file

def _remove_overlaps(in_file, out_dir, data):
    """Remove regions that overlap with next region, these result in issues with PureCN.
    """
    out_file = os.path.join(out_dir, "%s-nooverlaps%s" % utils.splitext_plus(os.path.basename(in_file)))
    if not utils.file_uptodate(out_file, in_file):
        with file_transaction(data, out_file) as tx_out_file:
            with open(in_file) as in_handle:
                with open(tx_out_file, "w") as out_handle:
                    prev_line = None
                    for line in in_handle:
                        if prev_line:
                            pchrom, pstart, pend = prev_line.split("\t", 4)[:3]
                            cchrom, cstart, cend = line.split("\t", 4)[:3]
                            # Skip if chromosomes match and end overlaps start
                            if pchrom == cchrom and int(pend) > int(cstart):
                                pass
                            else:
                                out_handle.write(prev_line)
                        prev_line = line
                    out_handle.write(prev_line)
    return out_file

def _get_purecn_files(paired, work_dir):
    """Retrieve organized structure of PureCN output files.
    """
    out_base = os.path.join(work_dir, "%s-purecn" % (dd.get_sample_name(paired.tumor_data)))
    out = {"plot": {}}
    all_files = []
    for plot in ["chromosomes", "local_optima", "segmentation", "summary"]:
        if plot == "summary":
            cur_file = "%s.pdf" % out_base
        else:
            cur_file = "%s_%s.pdf" % (out_base, plot)
        out["plot"][plot] = cur_file
        all_files.append(os.path.basename(cur_file))
    for key, ext in [["summary", ".csv"], ["dnacopy", "_dnacopy.seg"], ["genes", "_genes.csv"],
                     ["log", ".log"], ["loh", "_loh.csv"], ["rds", ".rds"],
                     ["variants", "_variants.csv"]]:
        cur_file = "%s%s" % (out_base, ext)
        out[key] = cur_file
        all_files.append(os.path.basename(cur_file))
    return out_base, out, all_files

def _sv_workdir(data):
    return utils.safe_makedir(os.path.join(dd.get_work_dir(data), "structural",
                                           dd.get_sample_name(data), "purecn"))
