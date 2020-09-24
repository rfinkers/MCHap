import sys
import argparse

from mchap.application import assemble
from mchap.application import pedigraph

def main():
    parser = argparse.ArgumentParser(
        'Bayesian assemby of micro-haplotypes in polyploids'
    )

    subprograms = ['assemble', 'pedigraph']
    parser.add_argument('program',
                        nargs=1,
                        choices=subprograms,
                        help='Specify sub-program')
    if len(sys.argv) < 2:
        parser.print_help()
    
    else:
        args = parser.parse_args(sys.argv[1:2])
        prog = args.program[0]
        if prog == 'assemble':
            prog = assemble.program
        elif prog == 'pedigraph':
            prog = pedigraph.program
        prog.cli(sys.argv)
