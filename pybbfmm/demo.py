import requests
import zipfile
import aljpy
import yaml
import numpy as np
from io import BytesIO
import torch
from aljpy import arrdict 
from . import adaptive
from tqdm.auto import tqdm

LOGIN = 'https://data-package.ceh.ac.uk/sso/login'
DATA = 'https://data-package.ceh.ac.uk/data/0995e94d-6d42-40c1-8ed4-5090d82471e1.zip'

def credentials():
    return yaml.safe_load(open('credentials.yml', 'r'))['CEH']

@aljpy.autocache()
def _density_map():
    with requests.Session() as sess:
        r = sess.post(LOGIN, data={**credentials(), 'success': '/data/0995e94d-6d42-40c1-8ed4-5090d82471e1', 'failure': '/'})
        r.raise_for_status()
        r = sess.get(DATA)
        r.raise_for_status()
    return r.content

def density_map():
    """Fetches a 1km-resolution map of the UK's population density as a numpy array."""
    content = _density_map()
    with zipfile.ZipFile(BytesIO(content)) as zf:
        filename = '0995e94d-6d42-40c1-8ed4-5090d82471e1/data/UK_residential_population_2011_1_km.asc'
        asc = zf.read(filename)
    arr = np.loadtxt(BytesIO(asc), skiprows=6)
    arr[arr == -9999] = np.nan
    return arr

def pop_points(n=1e3):
    """Distribute the population around the country, returning a Px2 array of their coordinates relative to the 
    bottom-right corner."""
    density = density_map()
    density = density/np.nansum(density)
    density[np.isnan(density)] = 0.

    indices = np.random.choice(np.arange(density.size), (int(n),), p=density.flatten())
    subscripts = np.stack(np.unravel_index(indices, density.shape), -1)

    # Pick a uniformly random spot in the grid cell
    ij = subscripts + np.random.rand(len(subscripts), 2)
    xy = np.stack([ij[:, 1], len(density) - ij[:, 0]], -1)

    return xy

def risk_kernel(a, b):
    d = (a - b).pow(2).sum(-1).pow(.5)
    return .01 * 1/(1 + (d/4)**3)

def kernel(a, b):
    # We want to take products of non-infection kernels here
    return torch.log(1 - risk_kernel(a, b).clamp(None, .9999))

def nbody_problem(pop):
    prob = arrdict.arrdict(
        sources=pop,
        targets=pop,
        charges=np.zeros(len(pop))
    ).map(torch.as_tensor).float().cuda()
    prob['kernel'] = kernel
    return prob

def render(points, charges):
    pass

def run(n=10e3):
    pop = pop_points(n=n)
    prob = nbody_problem(pop)
    presoln = adaptive.presolve(prob)

    # Set patient zero
    presoln.scaled.charges[0] = 1.

    for t in tqdm(range(10)):
        log_nonrisk = adaptive.evaluate(**presoln)
        risk = 1 - torch.exp(log_nonrisk)
        
        rands = torch.rand_like(risk)
        presoln.scaled.charges = (rands < risk).float()