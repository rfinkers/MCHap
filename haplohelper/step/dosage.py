import numpy as np
from numba import jit

from haplohelper.step import util

@jit(nopython=True)
def dosage_step_n_options(dosage):
    """Calculate the number of alternative dosages within one steps distance.
    Dosages must include at least one copy of each unique haplotype.

    Parameters
    ----------
    dosage : array_like, int, shape (ploidy)
        Array of dosages.

    Returns
    -------
    n : int
        The number of dosages within one step of the current dosage (excluding the current dosage).
    
    Notes
    -----
    A value of `0` in the `dosage` array indicates that that haplotype is a duplicate of another.

    """
    n_donors = 0
    n_recievers = 0
    ploidy = len(dosage)
    for h in range(ploidy):
        if dosage[h] == 1:
            n_recievers += 1
        elif dosage[h] > 1:
            n_recievers += 1
            n_donors += 1
        else:
            # 0 is an empty slot
            pass
    return n_donors * (n_recievers - 1)


def dosage_step_options(dosage):
    """Calculate all alternative dosages within one steps distance.
    Dosages must include at least one copy of each unique haplotype.

    Parameters
    ----------
    dosage : array_like, int, shape (ploidy)
        Array of dosages.

    Returns
    -------
    dosage_options : array_like, int, shape (n, ploidy)
        Dosages within one step of the current (excluding the current dosage).
    
    Notes
    -----
    A value of `0` in the `dosage` array indicates that that haplotype is a duplicate of another.

    """
    
    ploidy = len(dosage)

    n_options = dosage_step_n_options(dosage)

    # array of dosage options as row-vectors
    dosage_options = np.empty((n_options, ploidy), dtype=np.int)
    dosage_options[:] = dosage
    
    option = 0
    
    for d in range(ploidy):
        if dosage[d] <= 1:
            # this is not a valid donor
            pass
        else:
            for r in range(ploidy):
                if r == d:
                    # can't donate to yourself
                    pass
                elif dosage[r] == 0:
                    # this is an empty gap
                    pass
                else:
                    # this is a valid reciever
                    # remove 1 copy from the donor and assign it to the reciever
                    dosage_options[option, d] -= 1
                    dosage_options[option, r] += 1
                    
                    # incriment to the next option
                    option += 1

    return dosage_options


@jit(nopython=True)
def log_likelihood_dosage(reads, genotype, dosage):
    """Log likelihood of observed reads given a genotype and a dosage of haplotypes within that genotype.

    Parameters
    ----------
    reads : array_like, float, shape (n_reads, n_base, n_nucl)
        Observed reads encoded as an array of probabilistic matrices.
    genotype : array_like, int, shape (ploidy, n_base)
        Set of haplotypes with base positions encoded as simple integers from 0 to n_nucl.
    dosage : array_like, int, shape (ploidy)
        Array of dosages.

    Returns
    -------
    llk : float
        Log-likelihood of the observed reads given the genotype.

    Notes
    -----
    A haplotype with a dosage of 0 will not count towards the log-likelihood.

    """

    n_haps, n_base = genotype.shape
    n_reads = len(reads)
    
    # n_haps is not necessarily the ploidy level in this function
    # the ploidy is the sum of the dosages
    # but a dosage must be provided for each hap
    ploidy = 0
    for h in range(n_haps):
        ploidy += dosage[h]
    
    llk = 0.0
    
    for r in range(n_reads):
        
        read_prob = 0
        
        for h in range(n_haps):
            
            dose = dosage[h]
            
            if dose == 0:
                # this hap is not used (e.g. it's a copy of another)
                pass
            else:
                
                read_hap_prod = 1.0
            
                for j in range(n_base):
                    i = genotype[h, j]

                    read_hap_prod *= reads[r, j, i]
                read_prob += (read_hap_prod/ploidy) * dose
        
        llk += np.log(read_prob)
                    
    return llk



def dosage_swap_step(genotype, reads, dosage, llk):
    """Dosage swap Gibbs sampler step for all haplotypes in a genotype.

    Parameters
    ----------
    genotype : array_like, int, shape (ploidy, n_base)
        Initial state of haplotypes with base positions encoded as simple integers from 0 to n_nucl.
    reads : array_like, float, shape (n_reads, n_base, n_nucl)
        Observed reads encoded as an array of probabilistic matrices.
    dosage : array_like, int, shape (ploidy)
        Array of initial dosages.        
    llk : float
        Log-likelihood of the initial haplotype state given the observed reads.
    
    Returns
    -------
    llk : float
        New log-likelihood of observed reads given the updated genotype dosage.

    Notes
    -----
    Variables `genotype` and `dosage` are updated in place.

    """
    # alternative dosage options
    alt_dosage_options = dosage_step_options(dosage)

    # number of options including the initial dosage
    n_options = len(alt_dosage_options) + 1
    
    # array to hold log-liklihood for each dosage option including initial
    llks = np.empty(n_options)
    
    # iterate through alternate dosage options and calculate log-likelihood
    for opt in range(0, n_options - 1):
        llks[opt] = log_likelihood_dosage(reads, genotype, alt_dosage_options[opt])

    # final option is initial dosage (no change)
    llks[-1] = llk

    # calculated denominator in log space
    log_denominator = llks[0]
    for opt in range(1, n_options):
        log_denominator = util.sum_log_prob(log_denominator, llks[opt])

    # calculate conditional probabilities
    conditionals = np.empty(n_options)
    for opt in range(n_options):
        conditionals[opt] = np.exp(llks[opt] - log_denominator)

    # ensure conditional probabilities are normalised 
    conditionals /= np.sum(conditionals)
    
    # choose new dosage based on conditional probabilities
    options = np.arange(n_options)
    choice = util.random_choice(options, conditionals)
    
    # update dosage
    if choice == (n_options - 1):
        # this is the final option and hence the initial dosage is chosen
        pass
    else:
        # set the new dosage
        dosage = alt_dosage_options[choice]
    
        # update the state of the haplotypes
        util.set_genotype_dosage(genotype, dosage)
    
    # return log-likelihood for the chosen dosage
    return llks[choice]
