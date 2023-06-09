########################################################################################################################
#                                                                                                                      #
#                                           Plasma Euler equations solver                                              #
#                                                                                                                      #
#                                          Lionel Cheng, CERFACS, 04.11.2020                                           #
#                                                                                                                      #
########################################################################################################################

import argparse
import yaml
import logging
from time import perf_counter
import os

import numpy as np
import matplotlib.pyplot as plt
import scipy.constants as co
from numba import njit
import seaborn as sns
from scipy import interpolate

from .euler import Euler
import PlasmaNet.common.profiles as pf
from ...poissonsolver.network import PoissonNetwork
from ...common.plot import plot_ax_scalar, plot_ax_scalar_1D, plot_ax_vector_arrow
from ...common.utils import create_dir, save_obj

sns.set_context('notebook', font_scale=1.0)


class PlasmaEuler(Euler):
    def __init__(self, config, logger=None):
        super().__init__(config)
        # Choose the way to solve poisson equation, either classic with linear system
        # or with analytical solution (2D Fourier series)
        self.poisson_type = config['poisson']['type']

        # Copy mesh entry into poisson for class init
        for key, value in config['mesh'].items():
            config['poisson'][key] = value

        if self.poisson_type == 'network':
            config['poisson']['geom'] = self.geom
            config['network']['eval'] = config['poisson']
            config['network']['casename'] = config['casename']
            self.poisson = PoissonNetwork(config['network'])

        if 'interpolate' in config['poisson']:
            self.interpol = True
            self.interp_kind = config['poisson']['interpolate']
            self.intp_res = config['network']['arch']['args']['input_res']
            self.x_intrp = np.linspace(self.xmin, self.xmax, self.intp_res)
            self.y_intrp = np.linspace(self.ymin, self.ymax, self.intp_res)
            self.poisson.res_scale = self.poisson.nnx_nn**2 / self.intp_res**2
        else:
            self.interpol = False

        self.m_e = co.m_e
        self.W = self.m_e * co.N_A
        self.n_back = config['init']['n_back']
        self.n_pert = config['init']['n_pert']

        if config['init']['func'] == 'gaussians':
            n_electron = getattr(pf, config['init']['func'])(self.X,
                                                             self.Y, config['init']['args']) + self.n_back
        else:
            n_electron = getattr(pf, config['init']['func'])(self.X,
                                                             self.Y, self.n_pert, *config['init']['args']) + self.n_back

        self.U[0] = self.m_e * n_electron

        self.omega_p = np.sqrt(self.n_back * co.e**2 / self.m_e / co.epsilon_0)  
        self.T_p = 2 * np.pi / self.omega_p                      
        self.nt_oscill = config['params']['nt_oscill']
        self.dt = 2 * np.pi / self.omega_p / self.nt_oscill

        if 'n_periods' in config['params']:
            self.n_periods = config['params']['n_periods']
            self.nit = int(self.n_periods * self.nt_oscill)
            self.end_time = self.n_periods * self.T_p
        elif 'end_time' in config['params']:
            self.nit = int(self.end_time / self.dt)
            self.n_periods = self.end_time / self.T_p
        else:
            self.end_time = self.nit * self.dt
            self.n_periods = self.end_time / self.T_p

        # Save every fraction of period
        if self.save_type == 'plasma_period':
            self.save_type = 'iteration'
            self.period = int(self.period * self.nt_oscill)

        self.time = np.zeros(self.nit)

        # Retrieve the domain average and the above 0.2 * max values of n_electron
        self.temporals = np.zeros((self.nit, 2))
        nep = n_electron - self.n_back
        self.temporal_indices = get_indices(nep, self.nny, self.nnx, 0.2)
        self.temporal_ampl = np.zeros(2)
        self.temporal_ampl[0] = self.domain_ave(nep)
        self.temporal_ampl[1] = np.mean(
            nep[self.temporal_indices[:, 0], self.temporal_indices[:, 1]])
        self.nep_max = np.max(nep)

        # datasets for deep-learning
        self.dl_save = config['output']['dl_save'] == 'yes'
        if self.dl_save:
            self.dl_dir = config['casename'] + 'dl_data/'
            self.dl_fig = self.dl_dir + 'figures/'
            create_dir(self.dl_dir)
            create_dir(self.dl_fig)
            self.potential_list = np.zeros((self.nit, self.nny, self.nnx))
            self.physical_rhs_list = np.zeros((self.nit, self.nny, self.nnx))
            # Compute fourier for 100 iterations
            self.dl_plot_period = int(0.1 * self.nit)
            self.fourier_period = int(0.01 * self.nit)

        if 'globals' in config['output']:
            globals_cfg = config['output']['globals']

            # Global figure for article, the list is the times at each we want the plots
            # in period units
            if 'fig' in globals_cfg:
                self.gfig = plt.figure(figsize=(11, 6))
                self.gperiod = [int(per * self.nt_oscill)
                                for per in globals_cfg['fig']]
                self.gindex = 0

            # Declaration of globals dictionary which holds, norm2 of signal, norm2 of FFT
            # for domain average and > 0.9 max points as well as onset of instabilities
            if globals_cfg['vars'] == 'yes':
                self.globals = dict()
                self.globals['casename'] = self.case_dir
                self.globals['nnx_sim'] = self.nnx
                self.globals['Lx_sim'] = self.Lx
                self.globals['init_profile'] = config['init']['func'] + \
                    str(config['init']['args'])

        # If a logger is given, use it to log performance
        if logger is not None:
            self.logger = logger
            self.log_perf = True
        else:
            self.log_perf = False

    def print_init(self):
        """ Print header to sum up the parameters. """
        logging.info(
            f'Number of nodes: nnx = {self.nnx:d} -- nny = {self.nny:d}')
        logging.info(
            f'Bounding box: ({self.xmin:.1e}, {self.ymin:.1e}), ({self.xmax:.1e}, {self.ymax:.1e})')
        logging.info(f'dx = {self.dx:.2e} -- dy = {self.dy:.2e}')
        logging.info(
            f'dt = {self.dt:.2e} s - T_p = {self.T_p:.2e} s - omega_p = {self.omega_p:.2e} rad.s-1')
        logging.info('------------------------------------')
        logging.info('Start of simulation')
        logging.info('------------------------------------')
        logging.info('{:>10} {:>16} {:>17}'.format(
            'Iteration', 'Timestep [s]', 'Total time [s]', width=14))

    def solve_poisson(self, it: int):
        """ Solve the Poisson equation in axisymmetric configuration. """
        poisson_timer = perf_counter()

        # Creation of rhs variable
        rhs_field = - (self.U[0] / self.m_e - self.n_back) * co.e / co.epsilon_0

        # Interpolation of rhs
        if self.interpol:
            f = interpolate.interp2d(self.x, self.y, rhs_field, kind=self.interp_kind)
            rhs_field = f(self.x_intrp, self.y_intrp)

        self.poisson_type == 'network'
        self.poisson.solve(rhs_field)


        # Interpolation of potential
        if self.interpol:
            f = interpolate.interp2d(
                self.x_intrp, self.y_intrp, self.poisson.potential, kind=self.interp_kind)
            self.poisson.potential = f(self.x, self.y)

        self.E_field = self.poisson.E_field

        if self.it == 1:
            self.E_norm = np.sqrt(self.E_field[0]**2 + self.E_field[1]**2)
            self.E_max = np.max(self.E_norm)

        poisson_timer = perf_counter() - poisson_timer
        if self.log_perf:
            self.logger.info("Poisson {} perf: {}".format(
                self.poisson_type[:3], poisson_timer))

    def compute_flux_cold(self):
        """ Compute the 2D flux of the Euler equations but without pressure """
        F = self.F
        U = self.U
        compute_flux_cold(U, self.gamma, self.r, F)

    def compute_EM_source(self):
        """ Compute electro-magnetic source terms in vertex-centered approximation """
        self.res[1] += self.U[0] / self.m_e * \
            co.e * self.E_field[0] * self.voln
        self.res[2] += self.U[0] / self.m_e * \
            co.e * self.E_field[1] * self.voln
        self.res[3] += co.e * (self.U[1] * self.E_field[0] +
                               self.U[2] * self.E_field[1]) / self.m_e * self.voln

    def plot(self):
        """ 2D maps and 1D cuts at different y of the primitive variables. """
        n_e = self.U[0] / self.m_e - self.n_back
        E = self.E_field
        self.E_norm = np.sqrt(self.E_field[0]**2 + self.E_field[1]**2)
        E_norm = self.E_norm
        fig, axes = plt.subplots(nrows=2, ncols=2, figsize=(8, 8))
        plot_ax_scalar(fig, axes[0][0], self.X, self.Y, n_e,
                       r"$n_e$", geom='xy', max_value=1.1 * self.nep_max)
        plot_ax_scalar_1D(fig, axes[0][1], self.X, [0.4, 0.5, 0.6], n_e, r"$n_e$",
                          ylim=[-1.1 * self.nep_max, 1.1 * self.nep_max])
        plot_ax_vector_arrow(
            fig, axes[1][0], self.X, self.Y, E, 'Electric field', max_value=1.1 * self.E_max)
        plot_ax_scalar_1D(fig, axes[1][1], self.X, [0.25, 0.5, 0.75], E_norm, r"$|\mathbf{E}|$",
                          ylim=[0, 1.1 * self.E_max])
        fig.suptitle(rf'$t$ = {self.dtsum:.2e} s')
        fig.tight_layout()
        fig.savefig(self.fig_dir /
                    f'variables_{self.number:04d}', bbox_inches='tight')
        plt.close(fig)

        # Only 2D plotting
        fig, axes = plt.subplots(ncols=2, figsize=(8, 4))
        plot_ax_scalar(fig, axes[0], self.X, self.Y, n_e,
                       r"$n_e$", geom='xy', max_value=1.1 * self.nep_max)
        plot_ax_vector_arrow(
            fig, axes[1], self.X, self.Y, E, 'Electric field', max_value=1.1 * self.E_max)
        fig.suptitle(rf'$t$ = {self.dtsum:.2e} s')
        fig.tight_layout()
        fig.axes[0].get_xaxis().set_visible(False)
        fig.axes[0].get_yaxis().set_visible(False)
        fig.axes[1].get_xaxis().set_visible(False)
        fig.axes[1].get_yaxis().set_visible(False)
        fig.savefig(self.fig_dir /
                    f'variables_2D_{self.number:04d}', bbox_inches='tight')
        plt.close(fig)

    def postproc(self, it):
        super().postproc(it)
        if self.dl_save:
            self.potential_list[it - 1, :, :] = self.poisson.potential
            self.physical_rhs_list[it - 1, :, :] = self.poisson.physical_rhs
            if it % self.dl_plot_period == 0:
                self.poisson.plot_2D(self.dl_fig + f'input_{it:05d}')
            if it % self.fourier_period == 0:
                self.poisson.compute_modes()
        if hasattr(self, 'gfig'):
            if it == self.gperiod[self.gindex]:
                n_e = self.U[0] / self.m_e - self.n_back
                E = self.E_field
                tmp_ax1 = self.gfig.add_subplot(2, 3, 2 + self.gindex)
                plot_ax_scalar(self.gfig, tmp_ax1, self.X, self.Y, n_e,
                               rf"$n_e(t_{self.gindex + 1:d})$", geom='xy',
                               max_value=1.1 * self.nep_max,
                               cbar=True if self.gindex == len(self.gperiod) - 1 else False)
                tmp_ax2 = self.gfig.add_subplot(2, 3, 5 + self.gindex)
                plot_ax_vector_arrow(self.gfig, tmp_ax2, self.X, self.Y, E,
                                     rf"$\mathbf{{E}}(t_{self.gindex + 1:d})$",
                                     max_value=1.1 * self.E_max,
                                     cbar=True if self.gindex == len(self.gperiod) - 1 else False)

                self.gindex = min(self.gindex + 1, len(self.gperiod) - 1)

    def temporal_variables(self, it):
        """ Taking temporal variables in the middle for the single point for nnx
        odd or for the mean of the four points for nnx even """
        nep = self.U[0, :, :] / self.m_e - self.n_back
        self.temporals[it - 1, 0] = self.domain_ave(nep)
        self.temporals[it - 1, 1] = np.mean(
            nep[self.temporal_indices[:, 0], self.temporal_indices[:, 1]])

    def save(self):
        pass

    @staticmethod
    def ax_prop(ax, xlabel, ylabel, axtitle, ylim=None, xlim=None, legend=True):
        ax.grid(True)
        ax.set_xlabel(xlabel)
        ax.set_ylabel(ylabel)
        ax.set_title(axtitle)
        if legend:
            ax.legend()
        if ylim is not None:
            ax.set_ylim(ylim)
        if xlim is not None:
            ax.set_xlim(xlim)

    @staticmethod
    def fft(signal, t):
        npts = len(t)
        dt = (t[-1] - t[0]) / (npts - 1)
        freq = np.fft.rfftfreq(npts, dt)
        fft_signal = np.abs(np.fft.rfft(signal))**2
        return freq, fft_signal

    @staticmethod
    def norm2(y1, y2):
        return np.sqrt(np.sum((y1 - y2)**2)) / len(y1)

    def plot_temporal(self):
        """ Plot the global temporals with time normalized to plasma period units.
        Apply the normalization on temporals as well """
        self.time /= self.T_p
        self.temporals[:, 0] /= self.temporal_ampl[0]
        self.temporals[:, 1] /= self.temporal_ampl[1]
        self.omega_p *= self.T_p

        fig, axes = plt.subplots(ncols=2, nrows=2, figsize=(12, 10))
        axes = axes.reshape(-1)
        axes[0].plot(self.time, self.temporals[:, 0], label='Simulation')
        axes[1].plot(self.time, self.temporals[:, 1], 'k', label='Simulation')

        exact_cos = np.cos(self.omega_p * self.time)
        axes[0].plot(self.time, exact_cos, label='Reference')
        axes[1].plot(self.time, exact_cos, label='Reference')

        self.ax_prop(axes[0], '$t / T_p$', r"$n_e$ [m$^{-3}$]", r'Domain average of $n_e$',
                     ylim=[-1.5, 1.5])
        self.ax_prop(axes[1], '$t / T_p$', r"$n_e$ [m$^{-3}$]", r"$> 0.9\mathrm{max}(n_e)$",
                     ylim=[-1.5, 1.5])

        freq, fft_nep_de = self.fft(self.temporals[:, 0], self.time)
        freq *= 2 * np.pi / self.omega_p
        axes[2].plot(freq, fft_nep_de, label='Simulation')
        _, fft_ref_de = self.fft(exact_cos, self.time)
        axes[2].plot(freq, fft_ref_de, label='Reference')

        _, fft_nep_max = self.fft(self.temporals[:, 1], self.time)
        axes[3].plot(freq, fft_nep_max, 'k', label='Simulation')
        _, fft_ref_max = self.fft(exact_cos, self.time)
        axes[3].plot(freq, fft_ref_max, label='Reference')

        self.ax_prop(axes[2], r'$f / f_p$', "",
                     r"PSD of domain average of $n_e$", xlim=[0, 5])
        self.ax_prop(axes[3], r'$f / f_p$', "",
                     r"PSD of $> 0.9\mathrm{max}(n_e)$", xlim=[0, 5])

        fig.savefig(self.fig_dir / 'temporals', bbox_inches='tight')

        if hasattr(self, 'gfig'):
            # Global plot of temporals + 2D snapshots
            rect = 0.05, 0.25, 0.3, 0.5
            tmp_ax = self.gfig.add_axes(rect)
            tmp_ax.plot(self.time, self.temporals[:, 0], color='k')
            tmp_ax.plot(self.time, self.temporals[:, 1], 'k--')

            self.ax_prop(tmp_ax, '$t / T_p$', r"$n_e / n_e^\mathrm{max}$", '',
                         xlim=[0, self.n_periods], ylim=[-1.5, 1.5],
                         legend=False)
            tmp_ax.grid(False)

            # The barplots count as axis
            # 0 2 3(cbar)
            # 1 4 5(cbar)
            for ifig in [0, 1, 2, 4]:
                self.gfig.axes[ifig].get_xaxis().set_visible(False)
                self.gfig.axes[ifig].get_yaxis().set_visible(False)

            for i in range(2, 6):
                tmp_pos = self.gfig.axes[i].get_position()
                tmp_pos.x0 -= 0.04
                tmp_pos.x1 -= 0.04
                self.gfig.axes[i].set_position(tmp_pos)

            for i in range(len(self.gperiod)):
                tmp_ind = self.gperiod[i]
                tmp_time = self.time[tmp_ind]
                tmp_ne = self.temporals[tmp_ind, 0]
                self.gfig.axes[-1].text(tmp_time - 0.04 * self.time[-1], -
                                        1.35, rf"$t_{i + 1:d}$", ha='right', size=15)
                tmp_ax.plot(tmp_time * np.ones(2),
                            [-1.5, tmp_ne], 'k--', lw=1.0)

            self.gfig.savefig(self.fig_dir / 'global', bbox_inches='tight')

        if hasattr(self, 'globals'):
            self.globals['error_temporal_de'] = self.norm2(
                self.temporals[:, 0], exact_cos)
            self.globals['error_spectral_de'] = self.norm2(
                fft_nep_de, fft_ref_de)
            self.globals['error_temporal_max'] = self.norm2(
                self.temporals[:, 1], exact_cos)
            self.globals['error_spectral_max'] = self.norm2(
                fft_nep_max, fft_ref_max)

    def post_temporal(self):
        """ Saving of different files at the end of simulation """
        self.plot_temporal()
        if hasattr(self, 'globals'):
            save_obj(self.globals, os.path.join(self.case_dir,'globals'))
        if self.dl_save:
            np.save(self.dl_dir + 'potential.npy', self.potential_list)
            np.save(self.dl_dir + 'physical_rhs.npy', self.physical_rhs_list)
            self.poisson.plot_pmodes(self.dl_fig + 'modes')

    @classmethod
    def run(cls, config):
        """ Main function containing initialization, temporal loop and outputs. Takes a config dict as input. """
        sim = cls(config)
        # Print header to sum up the parameters
        if sim.verbose:
            sim.print_init()

        # Iterations
        for it in range(1, sim.nit + 1):
            sim.it = it
            sim.dtsum += sim.dt
            sim.time[it - 1] = sim.dtsum

            # Update of the residual to zero
            sim.res[:], sim.res_c[:] = 0, 0

            # Solve poisson equation
            sim.solve_poisson(it)

            # Compute euler fluxes (without pressure)
            # sim.compute_flux()
            sim.compute_flux_cold()

            # Compute residuals in cell-vertex method
            sim.compute_res()

            # Compute residuals from electro-magnetic terms
            sim.compute_EM_source()

            # boundary conditions
            sim.impose_bc_euler()

            # Apply residual
            sim.update_res()

            # Post processing
            sim.postproc(it)

            # Retrieve center variables
            sim.temporal_variables(it)

        # Plot temporals
        sim.post_temporal()


@njit(cache=True)
def compute_flux_cold(U, gamma, r, F):
    """ Compute the 2D flux of the Euler equations
    assuming a zero pressure (and temperature). """
    # rhou - rhov
    F[0, 0] = U[1]
    F[0, 1] = U[2]
    # rho u^2 + p - rho u v
    F[1, 0] = U[1]**2 / U[0]
    F[1, 1] = U[1] * U[2] / U[0]
    # rho u^2 + p - rho u v
    F[2, 0] = U[1] * U[2] / U[0]
    F[2, 1] = U[2]**2 / U[0]
    # u(rho E + p) - v(rho E + p)
    F[3, 0] = U[1] / U[0] * U[3]
    F[3, 1] = U[2] / U[0] * U[3]


@njit(cache=True)
def get_indices(profile, nny, nnx, threshold):
    """ Return the indices [j, i] associated to the number of points which
    are strictly above threshold * max of the profile """
    indices = []
    max_profile = np.max(np.abs(profile))
    for i in range(nnx):
        for j in range(nny):
            if profile[j, i] > threshold * max_profile:
                indices.append([j, i])
    return np.array(indices)


def main():
    args = argparse.ArgumentParser(description='PlasmaEuler oscillation run')
    args.add_argument('-c', '--config', required=True, type=str,
                      help='config file path (default: None)')
    args = args.parse_args()

    with open(args.config, 'r') as yaml_stream:
        config = yaml.safe_load(yaml_stream)

    PlasmaEuler.run(config)


if __name__ == '__main__':
    main()
