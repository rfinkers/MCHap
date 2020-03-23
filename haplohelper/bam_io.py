#!/usr/bin/env python3

import numpy as np
from collections import Counter as _Counter
from functools import reduce as _reduce
from operator import add as _add
from haplohelper import util
import biovector

# NGS error rate per base estimated by Pfeiffer et al 2018
# "Systematic evaluation of error rates and causes in short samples in next-generation sequencing"
Pfeiffer_2018_error = .0024

# pileupcolumn functions

def _column_reference(pileupcolumn):
    ref_char = '-'
    for read in pileupcolumn.pileups:
        for _, ref_pos, char in read.alignment.get_aligned_pairs(with_seq=True):
            if ref_pos == pileupcolumn.pos:
                ref_char = char.upper()
                break
        if ref_char != '-':
            break
    return ref_char


def _column_variants(pileupcolumn, min_map_qual=0):
    samples = dict()

    for pileupread in pileupcolumn.pileups:
        if (not pileupread.is_del
                and not pileupread.is_refskip
                and not pileupread.alignment.is_duplicate
                and pileupread.alignment.mapq >= min_map_qual):
            sample = pileupread.alignment.get_tag('RG')
            nucl = pileupread.alignment.query_sequence[
                pileupread.query_position]

            if sample in samples:
                samples[sample].update(nucl)
            else:
                samples[sample] = _Counter(nucl)

    return samples


# functions for using the result of _column_variants

def _check_proportion(counter, threshold):
    if len(counter) < 2:
        return False
    else:
        common = _reduce(max, counter.values())
        total = _reduce(_add, counter.values())
        if (total - common) / total >= threshold:
            return True
        else:
            return False


def _select_column_variants(samples,
                            min_mean_depth=0,
                            pop_min_proportion=0.0,
                            sample_min_proportion=0.0):
    selected = False

    totals = _reduce(_add, samples.values(), _Counter())

    if len(totals) > 1:
        depths = [sum(sample.values()) for sample in samples.values()]

        if np.mean(depths) >= min_mean_depth:

            if _check_proportion(totals, pop_min_proportion):
                selected = True
            else:
                variable = [_check_proportion(sample, sample_min_proportion)
                            for sample in samples.values()]
                if any(v and (d >= min_mean_depth) for v, d in zip(variable,
                                                                   depths)):
                    selected = True

    return selected

# functions for encoding reads

def _extract_column(pileupcolumn, min_map_qual=0):

    for pileupread in pileupcolumn.pileups:
        if (not pileupread.is_del
                and not pileupread.is_refskip
                and not pileupread.alignment.is_duplicate
                and pileupread.alignment.mapq >= min_map_qual):

            sample = pileupread.alignment.get_tag('RG')
            qname = pileupread.alignment.qname
            char = pileupread.alignment.query_sequence[
                pileupread.query_position]
            qual = pileupread.alignment.query_qualities[
                pileupread.query_position]

            yield sample, qname, char, qual


def _read_snp_dicts(alignment_file,
                    contig=None,
                    positions=None,
                    min_map_qual=20):

    # extract reads

    # sample: read: array of variants with phred scores
    data = dict()

    # maps reference position to array index
    pos_map = dict(zip(positions, range(len(positions))))

    # variants stored in array
    # default values for read gaps is a null allele 'N' with probability 1
    dtype_variant = np.dtype([('char', np.str_, 1), ('qual', np.int8)])
    template = np.empty(len(positions), dtype=dtype_variant)
    template['char'] = 'N'
    template['qual'] = 0

    for pileupcolumn in alignment_file.pileup(contig,
                                              np.min(positions),
                                              np.max(positions)):
        if pileupcolumn.pos in pos_map:
            pos = pileupcolumn.pos

            for sample, qname, char, qual in _extract_column(pileupcolumn,
                                                             min_map_qual):
                if sample not in data:
                    data[sample] = {}
                if qname not in data[sample]:
                    data[sample][qname] = template.copy()
                data[sample][qname][pos_map[pos]] = (char, qual)
    return data


def read_snps(alignment_file,
              contig=None,
              positions=None,
              min_map_qual=20):
    
    data = _read_snp_dicts(alignment_file, contig, positions, min_map_qual)

    sample_reads = {}
    sample_quals = {}

    for sample, d in data.items():
        n_reads = len(d)
        n_base = len(positions)
        strings = np.empty(n_reads, dtype='<U{0}'.format(n_base))
        quals = np.empty((n_reads, n_base), dtype=np.int8)
        for i, array in enumerate(d.values()):
            strings[i] = ''.join(array['char'])
            quals[i] = array['qual']
        sample_reads[sample] = strings
        sample_quals[sample] = quals
    
    return sample_reads, sample_quals






def encode_alignment_positions(alignment_file,
                               contig=None,
                               positions=None,
                               #alphabet=None,
                               min_map_qual=20):

    # extract reads

    # sample: read: array of variants with phred scores
    reads = dict()

    # maps reference position to array index
    pos_map = dict(zip(positions, range(len(positions))))

    # variants stored in array
    # default values for read gaps is a null allele 'N' with probability 1
    dtype_variant = np.dtype([('char', np.str_, 1), ('prob', np.float)])
    template = np.empty(len(positions), dtype=dtype_variant)
    template['char'] = 'N'
    template['prob'] = 1.0

    for pileupcolumn in alignment_file.pileup(contig,
                                              np.min(positions),
                                              np.max(positions)):
        if pileupcolumn.pos in pos_map:
            pos = pileupcolumn.pos

            for sample, qname, char, qual in _extract_column(pileupcolumn,
                                                             min_map_qual):
                if sample not in reads:
                    reads[sample] = {}
                if qname not in reads[sample]:
                    reads[sample][qname] = template.copy()
                reads[sample][qname][pos_map[pos]] = (
                    char,
                    util.prob_of_qual(qual)
                )
    return reads

"""     # encode reads (reuse same dict)
    for sample, data in reads.items():
        array = np.empty(
            (len(data), len(positions), alphabet.binary.nalleles()),
            dtype=np.float)
        for i, read in enumerate(data.values()):

            array[i] = alphabet.binary.encode(
                ''.join(read['char']),
                #probabilities=read['prob']
            )

        reads[sample] = array

    return reads """


# defaults for converting from Allelic to IUPAC
_DEFAULT_ALLELIC_TO_IUPAC = (
    ('N', 'N'),
    ('Z', 'Z'),
    ('.', '.'),
    ('-', '-'),
)

# defaults for converting from to IUPAC Allelic
_DEFAULT_IUPAC_TO_ALLELIC = (
    ('A', 'N'),
    ('C', 'N'),
    ('T', 'N'),
    ('G', 'N'),
    ('N', 'N'),
    ('Z', 'Z'),
    ('.', '.'),
    ('-', '-'),
)


class TranslatorEncoder(object):
    """Wraps an Encoder object and translates strings before/after
    calling encode/decode.
    """
    def __init__(self, encoder, translate_to_wrapped, translate_from_wrapped):
        assert len(translate_to_wrapped) == len(translate_from_wrapped)
        self.encoder=encoder
        self._translate_to_wrapped = translate_to_wrapped
        self._translate_from_wrapped = translate_from_wrapped

    def element_shape(self):
        return self.encoder.element_shape()

    def sequence_dims(self):
        return self.encoder.sequence_dims()

    def dtype(self):
        return self.encoder.dtype()

    def nalleles(self):
        return self.encoder.nalleles()

    def _translate(self, data, translator):
        if isinstance(data, str):
            return ''.join(translator[i][char] for i, char in enumerate(data))

        # assume as array of strings
        if not isinstance(data, np.ndarray):
            data = np.array(data)

        # copy to mutate in place
        data = data.copy()
        long = data.ravel()
        for j, string in enumerate(long):
            long[j] = ''.join(translator[i][char] for i, char in enumerate(string))
        return data

    def translate_to_wrapped(self, data):
        return self._translate(data, self._translate_to_wrapped)

    def translate_from_wrapped(self, data):
        return self._translate(data, self._translate_from_wrapped)

    def encode_string(self, string, dtype=None):
        # translate
        string = self.translate_to_wrapped(string)
        return self.encoder.encode_string(string, dtype=dtype)

    def decode_sequence(self, array):
        string = self.encoder.decode_sequence(array)
        # translate
        return self.translate_from_wrapped(string)

    def encode(self, string, dtype=None):
        # translate
        string = self.translate_to_wrapped(string)
        return self.encoder.encode(string, dtype=dtype)

    def decode(self, array, dtype=None):
        string = self.encoder.decode(array, dtype=dtype)
        # translate
        return self.translate_from_wrapped(string)




class VariantLociMap(object):
    """Block of variant positions
    """

    def __init__(self,
                 reference=None,
                 contig=None,
                 interval=None,
                 sequence=None,
                 n_alleles=None,
                 snps=None):  # (position, alleles) includes reference allele

        self.reference = reference
        self.contig = contig
        self._interval = interval  # interval translates seq pos to ref pos
        self._sequence = sequence
        self._n_alleles = n_alleles
        if n_alleles == 2:
            self.alphabet = biovector.alphabet.biallelic
        elif n_alleles == 3:
            self.alphabet = biovector.alphabet.triallelic
        elif n_alleles == 4:
            self.alphabet = biovector.alphabet.quadraallelic
        else:
            raise ValueError('n_alleles must be 2, 3, or 4')

        # template for creating full sequences
        snp_positions = {snp[0] for snp in snps}
        self._sequence_template = ''.join(char if pos not in snp_positions
                                          else '{}'
                                          for pos, char in zip(interval,
                                                               sequence))

        assert len(interval) == len(sequence)

        # check snps and create translation dicts
        n_snps = len(snps)
        self._iupac_to_allelic = np.empty(n_snps, dtype='<O')
        self._allelic_to_iupac = np.empty(n_snps, dtype='<O')

        for i, (snp_pos, alleles) in enumerate(snps):
            assert snp_pos in interval

            # check alleles
            ref_idx = snp_pos - interval.start
            ref_char = sequence[ref_idx]
            assert ref_char == alleles[0]
            a2i = dict(_DEFAULT_ALLELIC_TO_IUPAC)
            i2a = dict(_DEFAULT_IUPAC_TO_ALLELIC)
            for integer, char in enumerate(alleles):
                a2i[str(integer)] = char
                i2a[char] = str(integer)
            self._allelic_to_iupac[i] = a2i
            self._iupac_to_allelic[i] = i2a
        
        self.binary = TranslatorEncoder(
            self.alphabet.binary,
            self._iupac_to_allelic,
            self._allelic_to_iupac
        )

        self.integer = TranslatorEncoder(
            self.alphabet.integer,
            self._iupac_to_allelic,
            self._allelic_to_iupac
        )

        self.intflag = TranslatorEncoder(
            self.alphabet.intflag,
            self._iupac_to_allelic,
            self._allelic_to_iupac
        )

        # store snp position and alleles
        snps_dtype = np.dtype(
            [('position', np.int), ('alleles', np.str_, n_alleles)])
        self._snps = np.array(snps, snps_dtype)

    def __repr__(self):
        header = '{0}\n{1}\nPOS\tREF\tALT\n'.format(self.reference,
                                                    self.contig)
        data = '\n'.join(
            '{0}\t{1}\t{2}'.format(snp[0], snp[1][0], snp[1][1:]) for snp in
            self._snps)
        return header + data

    @property
    def reference_sequence(self):
        return self._sequence

    @property
    def template_sequence(self):
        return self._sequence_template

    @property
    def reference_alleles(self):
        return tuple(alleles[0] for _, alleles in self._snps)

    @property
    def alternate_alleles(self):
        return tuple(alleles[1:] for _, alleles in self._snps)

    @property
    def snps(self):
        return self._snps.copy()

    @property
    def snp_positions(self):
        return self._snps['position'].copy()

    @classmethod
    def from_alignment_snps(cls,
                            alignment_file=None,
                            reference=None,
                            contig=None,
                            interval=None,
                            n_alleles=None,
                            snps=None):
        """"""
        if contig is None:
            raise ValueError('contig is required')

        if interval is None:
            interval = range(snps[0][0], snps[-1][0] + 1)

        pileup = alignment_file.pileup(contig, interval.start, interval.stop)

        ref_chars = np.empty(len(interval), dtype='U1')

        for column in pileup:
            if column.pos in interval:
                ref_char = _column_reference(column)
                ref_chars[column.pos - interval.start] = ref_char

        sequence = ''.join(ref_chars)

        return cls(reference=reference,
                   contig=contig,
                   interval=interval,
                   sequence=sequence,
                   n_alleles=n_alleles,
                   snps=snps)

    @classmethod
    def from_alignment_positions(cls,
                                 alignment_file=None,
                                 reference=None,
                                 contig=None,
                                 interval=None,
                                 positions=None,
                                 n_alleles=2,
                                 min_map_qual=20):
        """"""
        if contig is None:
            raise ('contig is required')

        if interval is None:
            interval = range(positions[0], positions[-1] + 1)

        pileup = alignment_file.pileup(contig, interval.start, interval.stop)

        ref_chars = np.empty(len(interval), dtype='U1')
        snps = np.empty(len(positions), dtype='<O')

        snp_idx = 0

        for column in pileup:
            if column.pos in interval:
                ref_char = _column_reference(column)
                ref_chars[column.pos - interval.start] = ref_char

                if column.pos in positions:
                    counts = _column_variants(column, min_map_qual)
                    if len(counts) == 0:
                        alleles = ref_char
                    else:
                        counts = _reduce(_add, counts.values())
                        counts = counts.most_common(n_alleles)
                        alleles = ref_char + ''.join(
                            char for char, _ in counts if char != ref_char)
                        # assert len(alleles) == n_alleles
                    snps[snp_idx] = (column.pos, alleles)
                    snp_idx += 1

        sequence = ''.join(ref_chars)

        return cls(reference=reference,
                   contig=contig,
                   interval=interval,
                   sequence=sequence,
                   n_alleles=n_alleles,
                   snps=snps)

    @classmethod
    def from_alignment_interval(cls,
                                alignment_file=None,
                                reference=None,
                                contig=None,
                                interval=None,
                                n_alleles=2,
                                min_map_qual=20,
                                min_mean_depth=10,
                                pop_min_proportion=0.1,
                                sample_min_proportion=0.2):

        if contig is None:
            raise ValueError('contig is required')

        pileup = alignment_file.pileup(contig, interval.start, interval.stop)

        ref_chars = np.empty(len(interval), dtype='U1')
        snps = []

        for column in pileup:
            if column.pos in interval:
                ref_char = _column_reference(column)
                ref_chars[column.pos - interval.start] = ref_char

                allel_counts = _column_variants(
                    column,
                    min_map_qual
                )
                selected = _select_column_variants(
                    allel_counts,
                    min_mean_depth,
                    pop_min_proportion,
                    sample_min_proportion
                )
                if selected:
                    allel_counts = _reduce(_add, allel_counts.values())
                    allel_counts = allel_counts.most_common(n_alleles)
                    alleles = ref_char + ''.join(char for char, _
                                                 in allel_counts
                                                 if char != ref_char)
                    snps.append((column.pos, alleles))

        sequence = ''.join(ref_chars)

        return cls(reference=reference,
                   contig=contig,
                   interval=interval,
                   sequence=sequence,
                   n_alleles=n_alleles,
                   snps=snps)
