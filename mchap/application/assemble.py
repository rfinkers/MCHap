import sys
import argparse
import numpy as np
from dataclasses import dataclass
import pysam
import multiprocessing as mp
from collections import OrderedDict

from mchap import mset
from mchap import combinatorics
from mchap.assemble import (
    DenovoMCMC,
    genotype_likelihoods,
    genotype_posteriors,
    call_posterior_haplotypes,
    alternate_dosage_posteriors,
    call_posterior_mode,
)
from mchap.assemble.util import (
    natural_log_to_log10,
    index_as_genotype_alleles,
    genotype_alleles_as_index,
)
from mchap.encoding import character, integer
from mchap.io import (
    Locus,
    read_bed4,
    extract_sample_ids,
    extract_read_variants,
    encode_read_alleles,
    encode_read_distributions,
    qual_of_prob,
    vcf,
)

import warnings

warnings.simplefilter("error", RuntimeWarning)


_LOCUS_ASSEMBLY_ERROR = (
    "Exception encountered at locus: '{name}', '{contig}:{start}-{stop}'."
)
_SAMPLE_ASSEMBLY_ERROR = (
    "Exception encountered when assembling sample '{sample}' from file '{bam}'."
)


class LocusAssemblyError(Exception):
    pass


class SampleAssemblyError(Exception):
    pass


@dataclass
class program(object):
    bed: str
    vcf: str
    ref: str
    samples: list
    sample_bams: dict
    sample_ploidy: dict
    sample_inbreeding: dict
    sample_mcmc_temperatures: dict
    region: str = None
    region_id: str = None
    read_group_field: str = "SM"
    base_error_rate: float = 0.0
    ignore_base_phred_scores: bool = False
    mapping_quality: int = 20
    skip_duplicates: bool = True
    skip_qcfail: bool = True
    skip_supplementary: bool = True
    mcmc_chains: int = 1
    mcmc_steps: int = 1000
    mcmc_burn: int = 500
    mcmc_alpha: float = 1.0
    mcmc_beta: float = 3.0
    mcmc_fix_homozygous: float = 0.999
    mcmc_recombination_step_probability: float = 0.5
    mcmc_partial_dosage_step_probability: float = 0.5
    mcmc_dosage_step_probability: bool = 1.0
    mcmc_incongruence_threshold: float = 0.60
    mcmc_llk_cache_threshold: int = 100
    haplotype_posterior_threshold: float = 0.2
    use_assembly_posteriors: bool = False
    report_genotype_likelihoods: bool = False
    report_genotype_posterior: bool = False
    n_cores: int = 1
    precision: int = 3
    random_seed: int = 42
    cli_command: str = None

    @classmethod
    def cli(cls, command):
        """Program initialisation from cli command

        e.g. `program.cli(sys.argv)`
        """
        parser = argparse.ArgumentParser("MCMC haplotype assembly")

        parser.add_argument(
            "--region",
            type=str,
            nargs=1,
            default=[None],
            help=(
                "Specify a single target region with the format contig:start-stop. "
                "This region will be a single variant in the output VCF. "
                "This argument can not be combined with the --targets argument."
            ),
        )

        parser.add_argument(
            "--region-id",
            type=str,
            nargs=1,
            default=[None],
            help=(
                "Specify an identifier for the locus specified with the "
                "--region argument. This id will be reported in the output VCF."
            ),
        )

        parser.add_argument(
            "--targets",
            type=str,
            nargs=1,
            default=[None],
            help=(
                "Bed file containing multiple genomic intervals for haplotype assembly. "
                "First three columns (contig, start, stop) are mandatory. "
                "If present, the fourth column (id) will be used as the variant id in "
                "the output VCF."
                "This argument can not be combined with the --region argument."
            ),
        )

        parser.add_argument(
            "--variants",
            type=str,
            nargs=1,
            default=[None],
            help=(
                "Tabix indexed VCF file containing SNP variants to be used in "
                "assembly. Assembled haplotypes will only contain the reference and "
                "alternate alleles specified within this file."
            ),
        )

        parser.add_argument(
            "--reference",
            type=str,
            nargs=1,
            default=[None],
            help="Indexed fasta file containing the reference genome.",
        )

        parser.add_argument(
            "--bam",
            type=str,
            nargs="*",
            default=[],
            help=(
                "A list of 0 or more bam files. "
                "Haplotypes will be assembled for all samples found within all "
                "listed bam files unless the --sample-list parameter is used."
            ),
        )

        parser.add_argument(
            "--bam-list",
            type=str,
            nargs=1,
            default=[None],
            help=(
                "A file containing a list of bam file paths (one per line). "
                "This can optionally be used in place of or combined with the --bam "
                "parameter."
            ),
        )

        parser.add_argument(
            "--sample-bam",
            type=str,
            nargs=1,
            default=[None],
            help=(
                "A file containing a list of samples with bam file paths. "
                "This can optionally be used in place the --bam and --bam-list "
                "parameters. This is faster than using those parameters when running "
                "many small jobs. "
                "An error will be thrown if a sample is not found within its specified "
                "bam file."
            ),
        )

        parser.add_argument(
            "--ploidy",
            type=int,
            nargs=1,
            default=[2],
            help=(
                "Default ploidy for all samples (default = 2). "
                "This value is used for all samples which are not specified using "
                "the --sample-ploidy parameter"
            ),
        )

        parser.add_argument(
            "--sample-ploidy",
            type=str,
            nargs=1,
            default=[None],
            help=(
                "A file containing a list of samples with a ploidy value "
                "used to indicate where their ploidy differs from the "
                "default value. Each line should contain a sample identifier "
                "followed by a tab and then an integer ploidy value."
            ),
        )

        parser.add_argument(
            "--sample-list",
            type=str,
            nargs=1,
            default=[None],
            help=(
                "Optionally specify a file containing a list of samples to "
                "haplotype (one sample id per line). "
                "This file also specifies the sample order in the output. "
                "If not specified, all samples in the input bam files will "
                "be haplotyped."
            ),
        )

        parser.add_argument(
            "--inbreeding",
            type=float,
            nargs=1,
            default=[0.0],
            help=(
                "Default inbreeding coefficient for all samples (default = 0.0). "
                "This value is used for all samples which are not specified using "
                "the --sample-inbreeding parameter."
            ),
        )

        parser.add_argument(
            "--sample-inbreeding",
            type=str,
            nargs=1,
            default=[None],
            help=(
                "A file containing a list of samples with an inbreeding coefficient "
                "used to indicate where their expected inbreeding coefficient "
                "default value. Each line should contain a sample identifier "
                "followed by a tab and then a inbreeding coefficient value "
                "within the interval [0, 1]."
            ),
        )

        parser.add_argument(
            "--base-error-rate",
            nargs=1,
            type=float,
            default=[0.0],
            help=(
                "Expected base error rate of read sequences (default = 0.0). "
                "This is used in addition to base phred-scores by default "
                "however base phred-scores can be ignored using the "
                "--ignore-base-phred-scores flag."
            ),
        )

        parser.set_defaults(ignore_base_phred_scores=False)
        parser.add_argument(
            "--ignore-base-phred-scores",
            dest="ignore_base_phred_scores",
            action="store_true",
            help=(
                "Flag: Ignore base phred-scores as a source of base error rate. "
                "This can improve MCMC speed by allowing for greater de-duplication "
                "of reads however an error rate > 0.0 must be specified with the "
                "--base-error-rate argument."
            ),
        )

        parser.add_argument(
            "--haplotype-posterior-threshold",
            type=float,
            nargs=1,
            default=[0.20],
            help=(
                "Posterior probability required for a haplotype to be included in "
                "the output VCF as an alternative allele. "
                "The posterior probability of each haplotype is assessed per individual "
                "and calculated as the probability of that haplotype being present "
                "with one or more copies in that individual."
                "A haplotype is included as an alternate allele if it meets this "
                "posterior probability threshold in at least one individual. "
                "This parameter is the main mechanism to control the number of "
                "alternate alleles in ech VCF record and hence the number of genotypes "
                "assessed when recalculating likelihoods and posterior distributions "
                "(default = 0.20)."
            ),
        )

        parser.set_defaults(use_assembly_posteriors=False)
        parser.add_argument(
            "--use-assembly-posteriors",
            dest="use_assembly_posteriors",
            action="store_true",
            help=(
                "Flag: Use posterior probabilities from each individuals "
                "assembly rather than recomputing posteriors based on the "
                "observed alleles across all samples. "
                "These posterior probabilities will be used to call genotypes "
                ", metrics related to the genotype, and the posterior "
                "distribution (GP field) if specified. "
                "This may lead to less robust genotype calls in the presence "
                "of multi-modality and hence it is recommended to run the "
                "simulation for longer or using parallel-tempering when "
                "using this option. "
                "This option may be more suitable than the default when calling "
                "haplotypes in unrelated individuals. "
            ),
        )

        parser.set_defaults(genotype_likelihoods=False)
        parser.add_argument(
            "--genotype-likelihoods",
            dest="genotype_likelihoods",
            action="store_true",
            help=("Flag: Report genotype likelihoods in the GL VCF field."),
        )

        parser.set_defaults(genotype_posteriors=False)
        parser.add_argument(
            "--genotype-posteriors",
            dest="genotype_posteriors",
            action="store_true",
            help=("Flag: Report genotype posterior probabilities in the GP VCF field."),
        )

        parser.add_argument(
            "--mapping-quality",
            nargs=1,
            type=int,
            default=[20],
            help=("Minimum mapping quality of reads used in assembly (default = 20)."),
        )

        parser.set_defaults(skip_duplicates=True)
        parser.add_argument(
            "--keep-duplicate-reads",
            dest="skip_duplicates",
            action="store_false",
            help=(
                "Flag: Use reads marked as duplicates in the assembly "
                "(these are skipped by default)."
            ),
        )

        parser.set_defaults(skip_qcfail=True)
        parser.add_argument(
            "--keep-qcfail-reads",
            dest="skip_qcfail",
            action="store_false",
            help=(
                "Flag: Use reads marked as qcfail in the assembly "
                "(these are skipped by default)."
            ),
        )

        parser.set_defaults(skip_supplementary=True)
        parser.add_argument(
            "--keep-supplementary-reads",
            dest="skip_supplementary",
            action="store_false",
            help=(
                "Flag: Use reads marked as supplementary in the assembly "
                "(these are skipped by default)."
            ),
        )

        parser.add_argument(
            "--mcmc-chains",
            type=int,
            nargs=1,
            default=[2],
            help="Number of independent MCMC chains per assembly (default = 2).",
        )

        parser.add_argument(
            "--mcmc-temperatures",
            type=float,
            nargs="*",
            default=[1.0],
            help=(
                "A list of inverse-temperatures to use for parallel tempered chains. "
                "These values must be between 0 and 1 and will automatically be sorted in "
                "ascending order. The cold chain value of 1.0 will be added automatically if "
                "it is not specified."
            ),
        )

        parser.add_argument(
            "--sample-mcmc-temperatures",
            type=str,
            nargs=1,
            default=[None],
            help=(
                "A file containing a list of samples with mcmc (inverse) temperatures. "
                "Each line of the file should start with a sample identifier followed by "
                "tab seperated numeric values between 0 and 1. "
                "The number of temperatures specified may vary between samples. "
                "Samples not listed in this file will use the default values specified "
                "with the --mcmc-temperatures argument."
            ),
        )

        parser.add_argument(
            "--mcmc-steps",
            type=int,
            nargs=1,
            default=[1500],
            help="Number of steps to simulate in each MCMC chain (default = 1500).",
        )

        parser.add_argument(
            "--mcmc-burn",
            type=int,
            nargs=1,
            default=[500],
            help="Number of initial steps to discard from each MCMC chain (default = 500).",
        )

        parser.add_argument(
            "--mcmc-fix-homozygous",
            type=float,
            nargs=1,
            default=[0.999],
            help=(
                "Fix alleles that are homozygous with a probability greater "
                "than or equal to the specified value (default = 0.999). "
                "The probability of that a variant is homozygous in a sample is "
                "assessed independently for each variant prior to MCMC simulation. "
                'If an allele is "fixed" it is not allowed vary within the MCMC thereby '
                "reducing computational complexity."
            ),
        )

        parser.add_argument(
            "--mcmc-seed",
            type=int,
            nargs=1,
            default=[42],
            help=("Random seed for MCMC (default = 42). "),
        )

        parser.add_argument(
            "--mcmc-recombination-step-probability",
            type=float,
            nargs=1,
            default=[0.5],
            help=(
                "Probability of performing a recombination sub-step during "
                "each step of the MCMC. (default = 0.5)."
            ),
        )

        parser.add_argument(
            "--mcmc-partial-dosage-step-probability",
            type=float,
            nargs=1,
            default=[0.5],
            help=(
                "Probability of performing a within-interval dosage sub-step during "
                "each step of the MCMC. (default = 0.5)."
            ),
        )

        parser.add_argument(
            "--mcmc-dosage-step-probability",
            type=float,
            nargs=1,
            default=[1.0],
            help=(
                "Probability of performing a dosage sub-step during "
                "each step of the MCMC. (default = 1.0)."
            ),
        )

        parser.add_argument(
            "--mcmc-chain-incongruence-threshold",
            type=float,
            nargs=1,
            default=[0.60],
            help=(
                "Posterior phenotype probability threshold for identification of "
                "incongruent posterior modes (default = 0.60)."
            ),
        )

        parser.add_argument(
            "--mcmc-llk-cache-threshold",
            type=int,
            nargs=1,
            default=[100],
            help=(
                "Threshold for determining whether to cache log-likelihoods "
                "during MCMC to improve performance. This value is computed as "
                "ploidy * variants * unique-reads (default = 100). "
                "If set to 0 then log-likelihoods will be cached for all samples "
                "including those with few observed reads which is inefficient and "
                "can slow the MCMC. "
                "If set to -1 then log-likelihood caching will be disabled for all "
                "samples."
            ),
        )

        parser.add_argument(
            "--read-group-field",
            nargs=1,
            type=str,
            default=["SM"],
            help=(
                'Read group field to use as sample id (default = "SM"). '
                "The chosen field determines tha sample ids required in other "
                "input files e.g. the --sample-list argument."
            ),
        )

        parser.add_argument(
            "--cores",
            type=int,
            nargs=1,
            default=[1],
            help=("Number of cpu cores to use (default = 1)."),
        )

        if len(command) < 3:
            parser.print_help()
            sys.exit(1)
        args = parser.parse_args(command[2:])

        # target regions
        if (args.targets[0] is not None) and (args.region[0] is not None):
            raise ValueError("Cannot combine --targets and --region arguments.")

        # bam paths
        bams = args.bam
        if args.bam_list[0]:
            with open(args.bam_list[0]) as f:
                bams += [line.strip() for line in f.readlines()]
        if len(bams) != len(set(bams)):
            raise IOError("Duplicate input bams")
        if bams:
            sample_bams = extract_sample_ids(bams, id=args.read_group_field[0])

        # sample-bam file
        if args.sample_bam[0]:
            if bams:
                raise IOError(
                    "The --sample-bam argument cannot be combined with --bam or --bam-list."
                )
            sample_bams = dict()
            with open(args.sample_bam[0]) as f:
                for line in f.readlines():
                    sample, bam = line.strip().split("\t")
                    sample_bams[sample] = bam

        # samples
        if args.sample_list[0]:
            with open(args.sample_list[0]) as f:
                samples = [line.strip() for line in f.readlines()]
            # remove non-listed samples
            sample_bams = {s: sample_bams[s] for s in samples if s in sample_bams}
        else:
            # read samples from bam headers
            samples = list(sample_bams.keys())
        if len(samples) != len(set(samples)):
            raise IOError("Duplicate input samples")

        # sample ploidy where it differs from default
        sample_ploidy = dict()
        if args.sample_ploidy[0]:
            with open(args.sample_ploidy[0]) as f:
                for line in f.readlines():
                    sample, ploidy = line.strip().split("\t")
                    sample_ploidy[sample] = int(ploidy)

        # default ploidy
        for sample in samples:
            if sample in sample_ploidy:
                pass
            else:
                sample_ploidy[sample] = args.ploidy[0]

        # sample inbreeding where it differs from default
        sample_inbreeding = dict()
        if args.sample_inbreeding[0]:
            with open(args.sample_inbreeding[0]) as f:
                for line in f.readlines():
                    sample, inbreeding = line.strip().split("\t")
                    sample_inbreeding[sample] = float(inbreeding)

        # default inbreeding
        for sample in samples:
            if sample in sample_inbreeding:
                pass
            else:
                sample_inbreeding[sample] = args.inbreeding[0]

        # per sample mcmc temperatures
        sample_mcmc_temperatures = dict()
        if args.sample_mcmc_temperatures[0]:
            with open(args.sample_mcmc_temperatures[0]) as f:
                for line in f.readlines():
                    values = line.strip().split("\t")
                    sample = values[0]
                    temps = [float(v) for v in values[1:]]
                    temps.sort()
                    assert temps[0] > 0.0
                    assert temps[-1] <= 1.0
                    if temps[-1] != 1.0:
                        temps.append(1.0)
                    sample_mcmc_temperatures[sample] = temps

        # default mcmc temperatures
        temps = args.mcmc_temperatures
        temps.sort()
        assert temps[0] > 0.0
        assert temps[-1] <= 1.0
        if temps[-1] != 1.0:
            temps.append(1.0)
        for sample in samples:
            if sample in sample_mcmc_temperatures:
                pass
            else:
                sample_mcmc_temperatures[sample] = temps

        # must have some source of error in reads
        if args.ignore_base_phred_scores:
            if args.base_error_rate[0] == 0.0:
                raise ValueError(
                    "Cannot ignore base phred scores if --base-error-rate is 0"
                )

        return cls(
            bed=args.targets[0],
            vcf=args.variants[0],
            ref=args.reference[0],
            samples=samples,
            sample_bams=sample_bams,
            sample_ploidy=sample_ploidy,
            sample_inbreeding=sample_inbreeding,
            sample_mcmc_temperatures=sample_mcmc_temperatures,
            region=args.region[0],
            region_id=args.region_id,
            read_group_field=args.read_group_field[0],
            base_error_rate=args.base_error_rate[0],
            ignore_base_phred_scores=args.ignore_base_phred_scores,
            mapping_quality=args.mapping_quality[0],
            skip_duplicates=args.skip_duplicates,
            skip_qcfail=args.skip_qcfail,
            skip_supplementary=args.skip_supplementary,
            mcmc_chains=args.mcmc_chains[0],
            mcmc_steps=args.mcmc_steps[0],
            mcmc_burn=args.mcmc_burn[0],
            # mcmc_alpha,
            # mcmc_beta,
            mcmc_fix_homozygous=args.mcmc_fix_homozygous[0],
            mcmc_recombination_step_probability=args.mcmc_recombination_step_probability[
                0
            ],
            mcmc_partial_dosage_step_probability=args.mcmc_partial_dosage_step_probability[
                0
            ],
            mcmc_dosage_step_probability=args.mcmc_dosage_step_probability[0],
            mcmc_incongruence_threshold=args.mcmc_chain_incongruence_threshold[0],
            mcmc_llk_cache_threshold=args.mcmc_llk_cache_threshold[0],
            use_assembly_posteriors=args.use_assembly_posteriors,
            haplotype_posterior_threshold=args.haplotype_posterior_threshold[0],
            report_genotype_likelihoods=args.genotype_likelihoods,
            report_genotype_posterior=args.genotype_posteriors,
            n_cores=args.cores[0],
            cli_command=command,
            random_seed=args.mcmc_seed[0],
        )

    def loci(self):
        if (self.bed is None) and (self.region is None):
            raise ValueError("No region or targets bedfile is specified.")
        elif self.bed is not None:
            bed = read_bed4(self.bed)
            for b in bed:
                yield b.set_sequence(self.ref).set_variants(self.vcf)
        else:
            locus = Locus.from_region_string(self.region, self.region_id)
            yield locus.set_sequence(self.ref).set_variants(self.vcf)

    def _header_contigs(self):
        with pysam.Fastafile(self.ref) as fasta:
            contigs = [
                vcf.headermeta.ContigHeader(c, l)
                for c, l in zip(fasta.references, fasta.lengths)
            ]
        return contigs

    def header(self):

        # define vcf template
        meta_fields = [
            vcf.headermeta.fileformat("v4.3"),
            vcf.headermeta.filedate(),
            vcf.headermeta.source(),
            vcf.headermeta.phasing("None"),
            vcf.headermeta.commandline(self.cli_command),
            vcf.headermeta.randomseed(self.random_seed),
        ]

        contigs = self._header_contigs()

        filters = [
            vcf.filters.SamplePassFilter(),
        ]

        info_fields = [
            vcf.infofields.AN,
            vcf.infofields.AC,
            vcf.infofields.NS,
            vcf.infofields.DP,
            vcf.infofields.RCOUNT,
            vcf.infofields.END,
            vcf.infofields.NVAR,
            vcf.infofields.SNVPOS,
        ]

        format_fields = [
            vcf.formatfields.GT,
            vcf.formatfields.GQ,
            vcf.formatfields.PHQ,
            vcf.formatfields.DP,
            vcf.formatfields.RCOUNT,
            vcf.formatfields.RCALLS,
            vcf.formatfields.MEC,
            vcf.formatfields.KMERCOV,
            vcf.formatfields.GPM,
            vcf.formatfields.PHPM,
            vcf.formatfields.MCI,
        ]
        if self.report_genotype_likelihoods:
            format_fields += [vcf.formatfields.GL]
        if self.report_genotype_posterior:
            format_fields += [vcf.formatfields.GP]

        columns = [vcf.headermeta.columns(self.samples)]

        header = meta_fields + contigs + filters + info_fields + format_fields + columns
        return [str(line) for line in header]

    def encode_sample_reads(self, data):
        """Extract and encode reads from each sample at a locus.

        Parameters
        ----------
        data : LocusAssemblyData
            With `locus`, `samples`, `sample_bams`, and `sample_inbreeding`.

        Returns
        -------
        data : LocusAssemblyData
            With `sample_RCOUNT`,  `sample_DP`, `sample_read_calls`,
            `sample_read_dists_unique` and `sample_read_dist_counts`.
        """
        locus = data.locus
        for sample in data.samples:

            # path to bam for this sample
            path = data.sample_bams[sample]

            # wrap in try clause to pass sample info back with any exception
            try:

                # extract read data
                read_chars, read_quals = extract_read_variants(
                    data.locus,
                    path,
                    id=self.read_group_field,
                    min_quality=self.mapping_quality,
                    skip_duplicates=self.skip_duplicates,
                    skip_qcfail=self.skip_qcfail,
                    skip_supplementary=self.skip_supplementary,
                )[sample]

                # get read stats
                read_count = read_chars.shape[0]
                data.sample_RCOUNT[sample] = read_count
                read_variant_depth = character.depth(read_chars)
                if len(read_variant_depth) == 0:
                    # no variants to score depth
                    data.sample_DP[sample] = np.nan
                else:
                    data.sample_DP[sample] = np.round(np.mean(read_variant_depth))

                # encode reads as alleles and probabilities
                read_calls = encode_read_alleles(locus, read_chars)
                data.sample_read_calls[sample] = read_calls
                if self.ignore_base_phred_scores:
                    read_quals = None
                read_dists = encode_read_distributions(
                    locus,
                    read_calls,
                    read_quals,
                    error_rate=self.base_error_rate,
                )
                data.sample_RCALLS[sample] = np.sum(read_calls >= 0)

                # de-duplicate reads
                read_dists_unique, read_dist_counts = mset.unique_counts(read_dists)
                data.sample_read_dists_unique[sample] = read_dists_unique
                data.sample_read_dist_counts[sample] = read_dist_counts

            # end of try clause for specific sample
            except Exception as e:
                path = data.sample_bams.get(sample)
                message = _SAMPLE_ASSEMBLY_ERROR.format(sample=sample, bam=path)
                raise SampleAssemblyError(message) from e
        return data

    def assemble_sample_haplotypes(self, data):
        """De novo haplotype assembly of each sample.

        Parameters
        ----------
        data : LocusAssemblyData
            With `locus`, `samples`, `sample_ploidy`, `sample_inbreeding`,
            `sample_read_dists_unique` and `sample_read_dist_counts`.

        Returns
        -------
        data : LocusAssemblyData
            With `sample_mcmc_trace`,  `sample_mcmc_posterior` and `sample_MCI`.
        """
        for sample in data.samples:
            # wrap in try clause to pass sample info back with any exception
            try:
                trace = (
                    DenovoMCMC(
                        ploidy=data.sample_ploidy[sample],
                        n_alleles=data.locus.count_alleles(),
                        inbreeding=data.sample_inbreeding[sample],
                        steps=self.mcmc_steps,
                        chains=self.mcmc_chains,
                        fix_homozygous=self.mcmc_fix_homozygous,
                        recombination_step_probability=self.mcmc_recombination_step_probability,
                        partial_dosage_step_probability=self.mcmc_partial_dosage_step_probability,
                        dosage_step_probability=self.mcmc_dosage_step_probability,
                        temperatures=self.sample_mcmc_temperatures[sample],
                        random_seed=self.random_seed,
                        llk_cache_threshold=self.mcmc_llk_cache_threshold,
                    )
                    .fit(
                        data.sample_read_dists_unique[sample],
                        read_counts=data.sample_read_dist_counts[sample],
                    )
                    .burn(self.mcmc_burn)
                )
                data.sample_mcmc_trace[sample] = trace
                data.sample_mcmc_posterior[sample] = trace.posterior()
                data.sample_MCI[sample] = trace.replicate_incongruence(
                    threshold=self.mcmc_incongruence_threshold
                )

            # end of try clause for specific sample
            except Exception as e:
                path = data.sample_bams.get(sample)
                message = _SAMPLE_ASSEMBLY_ERROR.format(sample=sample, bam=path)
                raise SampleAssemblyError(message) from e
        return data

    def call_posterior_haplotypes(self, data):
        """Call vcf haplotypes based on haplotype posterior probabilities
        within each samples de novo assembly.

        Parameters
        ----------
        data : LocusAssemblyData
            With `sample_mcmc_posterior`.

        Returns
        -------
        data : LocusAssemblyData
            With `vcf_haplotypes`.
        """
        threshold = self.haplotype_posterior_threshold
        posteriors = list(data.sample_mcmc_posterior.values())
        data.vcf_haplotypes = call_posterior_haplotypes(posteriors, threshold=threshold)
        return data

    def encode_sample_assembly_posterior(self, data):
        """Encodes each samples assembly posterior as its reported posterior distribution
        in the VCF output field. If likelihoods are reported they will be calculated from
        the called haplotypes.

        Parameters
        ----------
        data : LocusAssemblyData
            With `vcf_haplotypes`, `samples`, `sample_ploidy` `sample_read_dists_unique`,
            `sample_read_dist_counts`, `sample_mcmc_posterior`.

        Returns
        -------
        data : LocusAssemblyData
            With `sample_GL`, `sample_GP`.
        """
        # map of VCF haplotype bytes to allele number
        haplotype_labels = {h.tobytes(): i for i, h in enumerate(data.vcf_haplotypes)}
        for sample in data.samples:
            # wrap in try clause to pass sample info back with any exception
            try:
                # only need to calculate likelihoods if they are reported
                if self.report_genotype_likelihoods:
                    llks = genotype_likelihoods(
                        reads=data.sample_read_dists_unique[sample],
                        read_counts=data.sample_read_dist_counts[sample],
                        ploidy=data.sample_ploidy[sample],
                        haplotypes=data.vcf_haplotypes,
                    )
                    data.sample_GL[sample] = np.round(
                        natural_log_to_log10(llks), self.precision
                    )
                # only need to encode posterior dist if reported
                if self.report_genotype_posterior:
                    # calculate size of posterior dist array
                    n_alleles = len(data.vcf_haplotypes)
                    ploidy = data.sample_ploidy[sample]
                    n_genotypes = combinatorics.count_unique_genotypes(
                        n_alleles, ploidy
                    )
                    posterior = np.zeros(n_genotypes, dtype=np.int32)
                    # encode the mcmc posterior as the vcf posterior
                    mcmc_post = data.sample_mcmc_posterior[sample]
                    for genotype, prob in zip(
                        mcmc_post.genotypes, mcmc_post.probabilities
                    ):
                        alleles = _genotype_as_alleles(genotype, haplotype_labels)
                        # cant encode incomplete genotypes
                        if np.all(alleles >= 0):
                            idx = genotype_alleles_as_index(alleles)
                            posterior[idx] = prob
                    data.sample_GP[sample] = np.round(posterior, self.precision)
            except Exception as e:
                path = data.sample_bams.get(sample)
                message = _SAMPLE_ASSEMBLY_ERROR.format(sample=sample, bam=path)
                raise SampleAssemblyError(message) from e
        return data

    def call_sample_posteriors(self, data):
        """Re-calculates and encodes each samples genotype likelihoods and posterior
        probabilities in VCF order based on the haplotypes called across all samples.

        Parameters
        ----------
        data : LocusAssemblyData
            With `vcf_haplotypes`, `samples`, `sample_ploidy`, `sample_inbreeding`,
            `sample_read_dists_unique`, `sample_read_dist_counts`,
            `sample_mcmc_posterior`.

        Returns
        -------
        data : LocusAssemblyData
            With `sample_log_likelihoods`, `sample_GL`, `sample_posterior_probs`, `sample_GP`.
        """
        for sample in data.samples:
            # wrap in try clause to pass sample info back with any exception
            try:
                # calculate likelihoods to generate posteriors
                llks = genotype_likelihoods(
                    reads=data.sample_read_dists_unique[sample],
                    read_counts=data.sample_read_dist_counts[sample],
                    ploidy=data.sample_ploidy[sample],
                    haplotypes=data.vcf_haplotypes,
                )
                data.sample_log_likelihoods[sample] = llks
                if self.report_genotype_likelihoods:
                    data.sample_GL[sample] = np.round(
                        natural_log_to_log10(llks), self.precision
                    )
                # calculate genotype posterior for called haplotypes
                posterior = genotype_posteriors(
                    log_likelihoods=llks,
                    ploidy=data.sample_ploidy[sample],
                    n_alleles=len(data.vcf_haplotypes),
                    inbreeding=data.sample_inbreeding[sample],
                )
                data.sample_posterior_probs[sample] = posterior
                if self.report_genotype_posterior:
                    data.sample_GP[sample] = np.round(posterior, self.precision)
            # end of try clause for specific sample
            except Exception as e:
                path = data.sample_bams.get(sample)
                message = _SAMPLE_ASSEMBLY_ERROR.format(sample=sample, bam=path)
                raise SampleAssemblyError(message) from e
        return data

    def call_sample_assembly_genotype(self, data):
        """Call sample genotype alleles and phenotype probs based on
        its assembly posterior.

        Parameters
        ----------
        data : LocusAssemblyData
            With `vcf_haplotypes`, `samples`, `sample_mcmc_posterior`.

        Returns
        -------
        data : LocusAssemblyData
            With `sample_genotype`, `sample_alleles`, `sample_GQ`, `sample_PHQ`,
            `sample_GPM`, `sample_PHPM`.
        """
        # map of VCF haplotype bytes to allele number
        haplotype_labels = {h.tobytes(): i for i, h in enumerate(data.vcf_haplotypes)}
        for sample in data.samples:
            # wrap in try clause to pass sample info back with any exception
            try:
                phenotype = data.sample_mcmc_posterior[sample].mode_phenotype()
                # genotype
                genotype, genotype_prob = phenotype.mode_genotype()
                alleles = _genotype_as_alleles(genotype, haplotype_labels)
                # phenotype
                phenotype_prob = phenotype.probabilities.sum()
                # genotype results
                data.sample_genotype[sample] = genotype
                data.sample_alleles[sample] = alleles
                data.sample_GQ[sample] = qual_of_prob(genotype_prob)
                data.sample_GPM[sample] = np.round(genotype_prob, self.precision)
                # phenotype results
                data.sample_PHPM[sample] = np.round(phenotype_prob, self.precision)
                data.sample_PHQ[sample] = qual_of_prob(phenotype_prob)
            except Exception as e:
                path = data.sample_bams.get(sample)
                message = _SAMPLE_ASSEMBLY_ERROR.format(sample=sample, bam=path)
                raise SampleAssemblyError(message) from e
        return data

    def call_sample_posterior_genotype(self, data):
        """Call sample genotype alleles and phenotype probs based on
        its posterior distribution over called haplotypes.

        Parameters
        ----------
        data : LocusAssemblyData
            With `vcf_haplotypes`, `samples`, `sample_ploidy`, `sample_posterior_probs`.

        Returns
        -------
        data : LocusAssemblyData
            With `sample_genotype`, `sample_alleles`, `sample_GQ`, `sample_PHQ`,
            `sample_GPM`, `sample_PHPM`.
        """
        for sample in data.samples:
            # wrap in try clause to pass sample info back with any exception
            try:
                # genotype
                idx = np.argmax(data.sample_posterior_probs[sample])
                genotype_prob = data.sample_posterior_probs[sample][idx]
                ploidy = data.sample_ploidy[sample]
                alleles = index_as_genotype_alleles(idx, ploidy)
                genotype = data.vcf_haplotypes[alleles]
                # phenotype
                _, phenotype_probs = alternate_dosage_posteriors(
                    alleles, data.sample_posterior_probs[sample]
                )
                # genotype results
                data.sample_genotype[sample] = genotype
                data.sample_alleles[sample] = alleles
                data.sample_GQ[sample] = qual_of_prob(genotype_prob)
                data.sample_GPM[sample] = np.round(genotype_prob, self.precision)
                # phenotype stats
                data.sample_PHPM[sample] = np.round(
                    phenotype_probs.sum(), self.precision
                )
                data.sample_PHQ[sample] = qual_of_prob(phenotype_probs.sum())
            except Exception as e:
                path = data.sample_bams.get(sample)
                message = _SAMPLE_ASSEMBLY_ERROR.format(sample=sample, bam=path)
                raise SampleAssemblyError(message) from e
        return data

    def call_sample_posterior_genotype_low_memory(self, data):
        """Call sample genotype alleles and phenotype probs based on
        its posterior distribution over called haplotypes.

        This method avoids using likelihood and posterior arrays calculated
        across all possible haplotypes.

        Parameters
        ----------
        data : LocusAssemblyData
            With `vcf_haplotypes`, `samples`, `sample_ploidy`, `sample_inbreeding`,
            `sample_read_dists_unique`, `sample_read_dist_counts`,

        Returns
        -------
        data : LocusAssemblyData
            With `sample_genotype`, `sample_alleles`, `sample_GQ`, `sample_PHQ`,
            `sample_GPM`, `sample_PHPM`.
        """
        for sample in data.samples:
            # wrap in try clause to pass sample info back with any exception
            try:
                # find mode
                alleles, _, genotype_prob, phenotype_prob = call_posterior_mode(
                    reads=data.sample_read_dists_unique[sample],
                    ploidy=data.sample_ploidy[sample],
                    haplotypes=data.vcf_haplotypes,
                    read_counts=data.sample_read_dist_counts[sample],
                    inbreeding=data.sample_inbreeding[sample],
                )
                genotype = data.vcf_haplotypes[alleles]
                # genotype results
                data.sample_genotype[sample] = genotype
                data.sample_alleles[sample] = alleles
                data.sample_GQ[sample] = qual_of_prob(genotype_prob)
                data.sample_GPM[sample] = np.round(genotype_prob, self.precision)
                # phenotype stats
                data.sample_PHQ[sample] = qual_of_prob(phenotype_prob)
                data.sample_PHPM[sample] = np.round(phenotype_prob, self.precision)
            except Exception as e:
                path = data.sample_bams.get(sample)
                message = _SAMPLE_ASSEMBLY_ERROR.format(sample=sample, bam=path)
                raise SampleAssemblyError(message) from e
        return data

    def compute_genotype_read_comparative_stats(self, data):
        """Computes some statistics comparing called genotypes and haplotypes
        to initial read sequences.

        Parameters
        ----------
        data : LocusAssemblyData
            With `vcf_haplotypes`, `samples`, `sample_read_calls`, `sample_genotype`.

        Returns
        -------
        data : LocusAssemblyData
            With `sample_MEC` and `sample_KMERCOV`.
        """
        for sample in data.samples:
            # wrap in try clause to pass sample info back with any exception
            try:
                genotype = data.sample_genotype[sample]
                read_calls = data.sample_read_calls[sample]
                data.sample_MEC[sample] = np.sum(
                    integer.minimum_error_correction(read_calls, genotype)
                )
                data.sample_KMERCOV[sample] = np.round(
                    integer.min_kmer_coverage(
                        read_calls,
                        genotype,
                        ks=[1, 2, 3],
                    ),
                    self.precision,
                )
            except Exception as e:
                path = data.sample_bams.get(sample)
                message = _SAMPLE_ASSEMBLY_ERROR.format(sample=sample, bam=path)
                raise SampleAssemblyError(message) from e
        return data

    def encode_sample_genotype_string(self, data):
        """Generate vcf GT string.

        Parameters
        ----------
        data : LocusAssemblyData
            With `samples` and `sample_alleles`.

        Returns
        -------
        data : LocusAssemblyData
            With `sample_GT`.
        """
        for sample in data.samples:
            # wrap in try clause to pass sample info back with any exception
            try:
                alleles = data.sample_alleles[sample]
                genotype_string = "/".join([str(a) if a >= 0 else "." for a in alleles])
                data.sample_GT[sample] = genotype_string
            except Exception as e:
                path = data.sample_bams.get(sample)
                message = _SAMPLE_ASSEMBLY_ERROR.format(sample=sample, bam=path)
                raise SampleAssemblyError(message) from e
        return data

    def get_record_fields(self, data):
        """Generate VCF record fields.

        Parameters
        ----------
        data : LocusAssemblyData
            With `locus`, `vcf_haplotypes`, `sample_alleles`,
            `sample_DP` and `sample_RCOUNT`.

        Returns
        -------
        data : LocusAssemblyData
            With `vcf_REF`, `vcf_ALTS`, `info_END`, `info_NVAR`, `info_SNVPOS`,
            `info_AC`, `info_AN`, `info_NS`, `info_DP` and `info_RCOUNT`.
        """
        # postions
        data.info_END = data.locus.stop
        data.info_NVAR = len(data.locus.variants)
        data.info_SNVPOS = np.subtract(data.locus.positions, data.locus.start) + 1
        # sequences
        vcf_allele_strings = data.locus.format_haplotypes(data.vcf_haplotypes)
        data.vcf_REF = vcf_allele_strings[0]
        data.vcf_ALTS = vcf_allele_strings[
            1:
        ]  # if len(vcf_allele_strings) > 0 else None
        # alt allele counts
        allele_counts = np.zeros(len(data.vcf_haplotypes), int)
        for array in data.sample_alleles.values():
            for a in array:
                if a >= 0:
                    allele_counts[a] += 1
        data.info_AC = allele_counts[1:]  # skip ref count
        # total number of alleles in called genotypes
        data.info_AN = np.sum(allele_counts > 0)
        # number of called samples
        data.info_NS = np.sum(
            [np.any(alleles >= 0) for alleles in data.sample_alleles.values()]
        )
        # total read depth and allele depth
        if len(data.locus.variants) == 0:
            # it will be misleading to return a depth of 0 in this case
            data.info_DP = np.nan
        else:
            data.info_DP = np.nansum(list(data.sample_DP.values()))
        # total read count
        data.info_RCOUNT = np.nansum(list(data.sample_RCOUNT.values()))
        return data

    def assemble_locus(self, locus):
        """Assembles samples at a locus and formats resulting data
        into a VCF record line.

        Parameters
        ----------
        locus
            Assembly target locus.
        samples : list
            Sample identifiers.
        sample_bams : dict
            Map for sample identifiers to bam path.
        sample_ploidy : dict
            Map of sample identifiers to ploidy.
        sample_inbreeding : dict
            Map of sample identifiers to inbreeding.

        Returns
        -------
        vcf_record : str
            VCF variant line.

        """
        data = LocusAssemblyData(
            locus=locus,
            samples=self.samples,
            sample_bams=self.sample_bams,
            sample_ploidy=self.sample_ploidy,
            sample_inbreeding=self.sample_inbreeding,
            report_genotype_likelihoods=self.report_genotype_likelihoods,
            report_genotype_posterior=self.report_genotype_posterior,
        )
        self.encode_sample_reads(data)
        self.assemble_sample_haplotypes(data)
        self.call_posterior_haplotypes(data)
        if self.use_assembly_posteriors:
            self.encode_sample_assembly_posterior(data)
            self.call_sample_assembly_genotype(data)
        elif self.report_genotype_likelihoods or self.report_genotype_posterior:
            self.call_sample_posteriors(data)
            self.call_sample_posterior_genotype(data)
        else:
            self.call_sample_posterior_genotype_low_memory(data)
        self.compute_genotype_read_comparative_stats(data)
        self.encode_sample_genotype_string(data)
        self.get_record_fields(data)
        return data.format_vcf_record()

    def _assemble_locus_wrapped(self, locus):
        try:
            result = self.assemble_locus(locus)
        except Exception as e:
            message = _LOCUS_ASSEMBLY_ERROR.format(
                name=locus.name, contig=locus.contig, start=locus.start, stop=locus.stop
            )
            raise LocusAssemblyError(message) from e
        return result

    def run(self):
        header = self.header()
        pool = mp.Pool(self.n_cores)
        jobs = ((locus,) for locus in self.loci())
        records = pool.starmap(self._assemble_locus_wrapped, jobs)
        return header + records

    def _worker(self, locus, queue):
        line = str(self._assemble_locus_wrapped(locus))
        queue.put(line)
        return line

    def _writer(self, queue):
        while True:
            line = queue.get()
            if line == "KILL":
                break
            sys.stdout.write(line + "\n")
            sys.stdout.flush()

    def _run_stdout_single_core(self):
        header = self.header()
        for line in header:
            sys.stdout.write(line + "\n")
        for locus in self.loci():
            line = self._assemble_locus_wrapped(locus)
            sys.stdout.write(line + "\n")

    def _run_stdout_multi_core(self):

        header = self.header()

        for line in header:
            sys.stdout.write(line + "\n")
        sys.stdout.flush()

        manager = mp.Manager()
        queue = manager.Queue()
        pool = mp.Pool(self.n_cores)

        # start writer process
        _ = pool.apply_async(self._writer, (queue,))

        jobs = []
        for locus in self.loci():
            job = pool.apply_async(self._worker, (locus, queue))
            jobs.append(job)

        for job in jobs:
            job.get()

        queue.put("KILL")
        pool.close()
        pool.join()

    def run_stdout(self):
        if self.n_cores <= 1:
            self._run_stdout_single_core()
        else:
            self._run_stdout_multi_core()


class LocusAssemblyData(object):
    def __init__(
        self,
        locus,
        samples,
        sample_bams,
        sample_ploidy,
        sample_inbreeding,
        report_genotype_likelihoods=False,
        report_genotype_posterior=False,
    ):
        self.locus = locus
        self.samples = samples  # list[str]
        self.sample_bams = sample_bams  # dict[str, str]
        self.sample_ploidy = sample_ploidy  # dict[str, int]
        self.sample_inbreeding = sample_inbreeding  # dict[str, float]
        self.report_genotype_likelihoods = report_genotype_likelihoods
        self.report_genotype_posterior = report_genotype_posterior

        # sample data
        # integer encoded reads
        self.sample_read_calls = dict()
        # unique probabalistic reads
        self.sample_read_dists_unique = dict()
        # unique probabalistic read counts
        self.sample_read_dist_counts = dict()
        # sample mcmc multi trace
        self.sample_mcmc_trace = dict()
        # sample mcmc posterior dist object
        self.sample_mcmc_posterior = dict()
        # sample mcm phenotype distribtion object
        self.sample_phenotype_dist = dict()
        # sample genotype call haplotypes
        self.sample_genotype = dict()
        # sample genotype allele numbers
        self.sample_alleles = dict()
        # sample log-likelihoods for G
        self.sample_log_likelihoods = dict()
        # sample posterior probabilities for G
        self.sample_posterior_probs = dict()
        # sample filter set
        self.sample_filters = dict()

        # vcf sample data
        self.sample_RCOUNT = dict()
        self.sample_DP = dict()
        self.sample_GPM = dict()
        self.sample_PHPM = dict()
        self.sample_RCALLS = dict()
        self.sample_GQ = dict()
        self.sample_PHQ = dict()
        self.sample_MEC = dict()
        self.sample_KMERCOV = dict()
        self.sample_GL = dict()
        self.sample_GP = dict()
        self.sample_GT = dict()
        self.sample_DOSEXP = dict()
        self.sample_MCI = dict()

        # vcf record data
        self.vcf_haplotypes = None
        self.vcf_REF = None
        self.vcf_ALTS = None
        self.info_AN = None
        self.info_AC = None
        self.info_NS = None
        self.info_DP = None
        self.info_RCOUNT = None
        self.info_END = None
        self.info_NVAR = None
        self.info_SNVPOS = None

    def _sample_dict_as_list(self, d):
        return [d.get(s) for s in self.samples]

    def format_vcf_record(self):
        vcf_INFO = vcf.format_info_field(
            AN=self.info_AN,
            AC=self.info_AC,
            NS=self.info_NS,
            DP=self.info_DP,
            RCOUNT=self.info_RCOUNT,
            END=self.info_END,
            NVAR=self.info_NVAR,
            SNVPOS=self.info_SNVPOS,
        )
        format_fields = OrderedDict()
        format_fields["GT"] = self._sample_dict_as_list(self.sample_GT)
        format_fields["GQ"] = self._sample_dict_as_list(self.sample_GQ)
        format_fields["PHQ"] = self._sample_dict_as_list(self.sample_PHQ)
        format_fields["DP"] = self._sample_dict_as_list(self.sample_DP)
        format_fields["RCOUNT"] = self._sample_dict_as_list(self.sample_RCOUNT)
        format_fields["RCALLS"] = self._sample_dict_as_list(self.sample_RCALLS)
        format_fields["MEC"] = self._sample_dict_as_list(self.sample_MEC)
        format_fields["KMERCOV"] = self._sample_dict_as_list(self.sample_KMERCOV)
        format_fields["GPM"] = self._sample_dict_as_list(self.sample_GPM)
        format_fields["PHPM"] = self._sample_dict_as_list(self.sample_PHPM)
        format_fields["MCI"] = self._sample_dict_as_list(self.sample_MCI)
        if self.report_genotype_likelihoods:
            format_fields["GL"] = self._sample_dict_as_list(self.sample_GL)
        if self.report_genotype_posterior:
            format_fields["GP"] = self._sample_dict_as_list(self.sample_GP)
        vcf_FORMAT = vcf.format_sample_field(**format_fields)
        return vcf.format_record(
            chrom=self.locus.contig,
            pos=self.locus.start + 1,  # 0-based BED to 1-based VCF
            id=self.locus.name,
            ref=self.vcf_REF,
            alt=self.vcf_ALTS,
            qual=None,
            filter=None,
            info=vcf_INFO,
            format=vcf_FORMAT,
        )


def _genotype_as_alleles(genotype, labels):
    """Convert a  genotype of haplotype arrays to an array
    of VCF sorted allele integers.
    Parameters
    ----------
    genotype : ndarray, int, shape (ploidy, n_positions)
        Integer encoded genotype.
    labels : dict[bytes, int]
        Map of haplotype bytes to allele number e.g.
        `{h.tobytes(): i for i, h in enumerate(haplotypes)}`.

    Returns
    -------
    alleles : ndarray, int, shape (ploidy, )
        VCF sorted alleles.
    """
    alleles = np.sort([labels.get(h.tobytes(), -1) for h in genotype])
    alleles = np.append(alleles[alleles >= 0], alleles[alleles < 0])
    return alleles
