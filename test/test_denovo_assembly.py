import pathlib

from haplokit.version import __version__
from haplokit.io.vcf.headermeta import filedate
from haplokit.application.denovo_assembly import program


def test_Program__cli():
    samples = ('SAMPLE1', 'SAMPLE2', 'SAMPLE3')

    path = pathlib.Path(__file__).parent.absolute()
    path = path / 'test_io/data'

    BED = str(path / 'simple.bed.gz')
    VCF = str(path / 'simple.vcf.gz')
    REF = str(path / 'simple.fasta')
    BAMS = [
        str(path / 'simple.sample1.deep.bam'),
        str(path / 'simple.sample2.deep.bam'),
        str(path / 'simple.sample3.deep.bam')
    ]

    command = [
        'haplokit-denovo',
        '--bam', BAMS[0], BAMS[1], BAMS[2],
        '--ploidy', '4',
        '--bed', BED,
        '--vcf', VCF,
        '--ref', REF,
        '--mcmc-steps', '500',
        '--mcmc-burn', '100',
        '--mcmc-seed', '11',
        '--cores', '5',
    ]

    prog = program.cli(command)

    assert prog.mcmc_steps == 500
    assert prog.mcmc_burn == 100
    assert prog.random_seed == 11
    assert prog.n_cores == 5
    assert prog.cli_command == command

    expect_sample_bam = dict(zip(samples, BAMS))
    assert len(prog.sample_bam) == len(expect_sample_bam)
    for k, v in expect_sample_bam.items():
        assert prog.sample_bam[k] == v

    expect_sample_ploidy = {sample: 4 for sample in samples}
    assert len(prog.sample_ploidy) == len(expect_sample_ploidy)
    for k, v in expect_sample_ploidy.items():
        assert prog.sample_ploidy[k] == v


def test_Program__header():
    path = pathlib.Path(__file__).parent.absolute()
    path = path / 'test_io/data'

    BED = str(path / 'simple.bed.gz')
    VCF = str(path / 'simple.vcf.gz')
    REF = str(path / 'simple.fasta')
    BAMS = [
        str(path / 'simple.sample1.deep.bam'),
        str(path / 'simple.sample2.deep.bam'),
        str(path / 'simple.sample3.deep.bam')
    ]

    command = [
        'haplokit-denovo',
        '--bam', BAMS[0], BAMS[1], BAMS[2],
        '--ploidy', '4',
        '--bed', BED,
        '--vcf', VCF,
        '--ref', REF,
        '--mcmc-steps', '500',
        '--mcmc-burn', '100',
        '--mcmc-seed', '11',
    ]

    prog = program.cli(command)
    header = prog.header()

    meta_expect = [
        '##fileformat=VCFv4.3',
        str(filedate()),
        '##source=Haplokit v{}'.format(__version__),
        '##phasing=None',
        '##commandline="{}"'.format(' '.join(command)),
        '##randomseed=11',
    ]
    meta_actual = [str(i) for i in header.meta]
    assert meta_actual == meta_expect

    contigs_expect = [
        '##contig=<ID=CHR1,length=60>',
        '##contig=<ID=CHR2,length=60>',
        '##contig=<ID=CHR3,length=60>',
    ]
    contigs_actual = [str(i) for i in header.contigs]
    assert contigs_actual == contigs_expect

    filters_expect = [
        '##FILTER=<ID=PASS,Description="All filters passed">',
        '##FILTER=<ID=3m95,Description="Less than 0.95 of read-variant 3-mers represented in haplotypes">',
        '##FILTER=<ID=dp5,Description="Sample has mean read depth less than 5.0">',
        '##FILTER=<ID=rc5,Description="Sample has read (pair) count of less than 5.0">',
        '##FILTER=<ID=pp95,Description="Samples phenotype posterior probability less than 0.95">',
    ]
    filters_actual = [str(i) for i in header.filters]
    assert filters_actual == filters_expect


    samples_expect = ('SAMPLE1', 'SAMPLE2', 'SAMPLE3')
    samples_actual = header.samples
    assert samples_actual == samples_expect

    columns_expect = ('CHROM', 'POS', 'ID', 'REF', 'ALT', 'QUAL', 'FILTER', 'INFO', 'FORMAT')
    columns_expect += samples_expect
    columns_actual = header.columns()
    assert columns_expect == columns_actual


def test_Program__run():
    path = pathlib.Path(__file__).parent.absolute()
    path = path / 'test_io/data'

    BED = str(path / 'simple.bed.gz')
    VCF = str(path / 'simple.vcf.gz')
    REF = str(path / 'simple.fasta')
    BAMS = [
        str(path / 'simple.sample1.deep.bam'),
        str(path / 'simple.sample2.deep.bam'),
        str(path / 'simple.sample3.deep.bam')
    ]

    command = [
        'haplokit-denovo',
        '--bam', BAMS[0], BAMS[1], BAMS[2],
        '--ploidy', '4',
        '--bed', BED,
        '--vcf', VCF,
        '--ref', REF,
        '--mcmc-steps', '500',
        '--mcmc-burn', '100',
        '--mcmc-seed', '11',
    ]

    samples = ('SAMPLE1', 'SAMPLE2', 'SAMPLE3')
    prog = program.cli(command)
    out = prog.run()
    assert out.header.samples == samples

    records = out.records
    record = records[0]
    assert record.chrom == 'CHR1'
    assert record.pos == 5
    assert record.id == 'CHR1_05_25'
    assert record.ref == 'A' * 20
    assert record.alt == ['AAAAAAAAAAGAAAAAATAA', 'ACAAAAAAAAGAAAAAACAA']

    info = record.info
    assert info['END'] == 25
    assert info['SNVPOS'] == '2,11,18'
    assert info['NS'] == 3
    assert tuple(info['AC']) == (3, 2)
    assert info['AN'] == 3

    format = record.format
    assert set(format.keys()) == set(samples)

    sample = format['SAMPLE1']
    assert sample['GPM'] == 1.0
    assert sample['PPM'] == 1.0
    assert sample['RC'] == 200
    assert sample['DP'] == 133
    assert sample['GQ'] == 60
    assert sample['PHQ'] == 60
    assert str(sample['GT']) == '0/0/1/2'
    assert sample['MPGP'] == [1.0, 0.0, 0.0]
    assert sample['MPED'] == [2.0, 1.0, 1.0]
    assert str(sample['FT']) == 'PASS'
