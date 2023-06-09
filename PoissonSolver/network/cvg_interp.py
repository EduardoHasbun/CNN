########################################################################################################################
#                                                                                                                      #
#                                 2D Poisson network convergence when interpolating                                    #
#                                                                                                                      #
#                                          Lionel Cheng, CERFACS, 14.09.2021                                           #
#                                                                                                                      #
########################################################################################################################

from PlasmaNet.common.operators_numpy import grad
from PlasmaNet.poissonsolver.network import PoissonNetwork
from scipy import interpolate
from pathlib import Path
import seaborn as sns
import scipy.constants as co
import matplotlib as mpl
import matplotlib.pyplot as plt
import numpy as np
import yaml
import argparse
import os
import time
import copy

os.environ['OPENBLAS_NUM_THREADS'] = '1'

def ax_prop(ax, ylabel, ymin, ymax):
    ax.grid(True)
    ax.set_xlabel(r'$n_\mathrm{pts}$')
    ax.set_ylabel(ylabel)
    ax.set_xscale('log')
    ax.set_yscale('log')
    ax.set_ylim(ymin, ymax)

def run(cfg: dict, interp_type: str, interp_kind: str):
    """ Run interpolation runs with interp_type = scipy or torch 
    and interp_kind = linear/cubic for scipy or bilinear/bicubic for torch"""
    # Print run
    print(f'Run {interp_kind} - {interp_type}')
    
    # Start time
    time_start = time.time()

    cfg['network']['eval'] = cfg['eval']

    fig_dir = Path(cfg['network']['casename']) / 'cvg_interp' / f'{interp_kind}_{interp_type}'
    fig_dir.mkdir(parents=True, exist_ok=True)
    cfg['network']['interp_kind'] = interp_kind

    # creating the rhs amplitude and the studied modes
    ni0 = 1e+11
    rhs0 = ni0 * co.e / co.epsilon_0
    modes = ((1, 1), (1, 2), (2, 1), (2, 2), (6, 6), (10, 10))
    modes_style = [{'color': 'darkblue', 'marker': 'o'}, {'color': 'royalblue', 'marker': 'o', 'linestyle': 'dashed'},
                   {'color': 'royalblue', 'marker': '^', 'linestyle': 'dashed'}, {
                       'color': 'royalblue', 'marker': 's', 'linestyle': 'dashed'},
                   {'color': 'firebrick', 'marker': 'o', 'linestyle': 'dotted'}, {'color': 'firebrick', 'marker': '^', 'linestyle': 'dotted'}]

    # Different studied resolutions
    nnxs = np.array([51, 101, 201, 401, 701, 1001, 2001])
    errors = {'L1': {}, 'L2': {}, 'Linf': {}}
    for key in errors:
        for (mode_n, mode_m) in modes:
            errors[key][f'({mode_n:d}, {mode_m:d})'] = {'potential': np.zeros(len(nnxs)),
                                                        'E_field': np.zeros(len(nnxs))}

    # Global values
    xmin, xmax, nnx_nn = cfg['network']['globals']['xmin'], cfg['network']['globals']['xmax'], \
                            cfg['network']['globals']['nnx']
    ymin, ymax, nny_nn = cfg['network']['globals']['ymin'], cfg['network']['globals']['ymax'], \
                            cfg['network']['globals']['nny']
    x_red = np.linspace(xmin, xmax, nnx_nn)
    y_red = np.linspace(ymin, ymax, nny_nn)

    # Loop on different resolutions
    for i_nnx, nnx in enumerate(nnxs):
        print(f'Case resolution nnx = {nnx:d}')
        nny = nnx

        # Change the configuration dict and initialize new poisson object with good res
        cfg['network']['eval']['nnx'] = nnx
        cfg['network']['eval']['nny'] = nny

        poisson = PoissonNetwork(cfg['network'])
        poisson.case_config(cfg['network']['eval'])
        poisson.res_scale = 1.0

        # interior rhs for exact solution it is the mode specified by nmax and mmax above
    

    # Creation of L1, L2 and Linf error figs
    for error_kind in errors:
        fig, axes = plt.subplots(ncols=2, figsize=(8, 5))
        for imode, (mode_n, mode_m) in enumerate(modes):
            axes[0].plot(nnxs, errors[error_kind][f'({mode_n:d}, {mode_m:d})']
                         ['potential'], label=f'({mode_n:d}, {mode_m:d})', **modes_style[imode])
        ax_prop(axes[0], r'$\epsilon_\phi$', 4.e-6, 4.e-3)
        for imode, (mode_n, mode_m) in enumerate(modes):
            axes[1].plot(nnxs, errors[error_kind][f'({mode_n:d}, {mode_m:d})']
                         ['E_field'], label=f'({mode_n:d}, {mode_m:d})', **modes_style[imode])
        ax_prop(axes[1], r'$\epsilon_\mathbf{E}$', 5.e-3, 3.e0)

        # Put global legend for both figures
        handles, labels = axes[1].get_legend_handles_labels()
        fig.legend(handles, labels, loc='center right', borderaxespad=0.1)
        fig.subplots_adjust(right=0.82)

        # Move left figure
        tmp_pos = fig.axes[0].get_position()
        tmp_pos.x0 -= 0.05
        tmp_pos.x1 -= 0.05
        fig.axes[0].set_position(tmp_pos)

        # fig.tight_layout()
        fig.savefig(fig_dir / f'{error_kind}_error.pdf',
                    format='pdf', bbox_inches='tight')
        fig.savefig(fig_dir / f'{error_kind}_error.png', bbox_inches='tight')
        plt.close(fig)

    time_stop = time.time()
    print(f'Elapsed time (s) : {time_stop - time_start:.3e}')

if __name__ == '__main__':
    # Plotting options for aesthetics
    sns.set_context('notebook', font_scale=1.1)
    lines_params = {'basic': {'linewidth': 2, 'markersize': 6}}
    line_style = {'marker': 'o'}
    mpl.rc('lines', **lines_params['basic'])

    args = argparse.ArgumentParser(description='PoissonNetwork runs')
    args.add_argument('-c', '--config', default=None, type=str,
                      help='Config file path (default: None)')
    args = args.parse_args()

    with open(args.config, 'r') as yaml_stream:
        cfg = yaml.safe_load(yaml_stream)

    run(copy.deepcopy(cfg), 'scipy', 'linear')
    run(copy.deepcopy(cfg), 'scipy', 'cubic')
    run(copy.deepcopy(cfg), 'torch', 'bilinear')
    run(copy.deepcopy(cfg), 'torch', 'bicubic')