# -*- coding: utf-8 -*-
"""
Created on: 05/11/2021
Updated on:

Original author: Ben Taylor
Last update made by: Ben Taylor
Other updates made by:

File purpose:

"""
# Built-Ins
import os
import abc
import time
import queue
import warnings
import operator
import functools
import dataclasses

from typing import Any
from typing import List
from typing import Dict
from typing import Tuple

# Third Party
import numpy as np
import pandas as pd
from scipy import optimize

# Local Imports
import normits_demand as nd

from normits_demand import cost

from normits_demand.utils import timing
from normits_demand.utils import file_ops
from normits_demand.utils import math_utils
from normits_demand.utils import general as du
from normits_demand.utils import pandas_utils as pd_utils

from normits_demand.validation import checks
from normits_demand.distribution import furness
from normits_demand.cost import utils as cost_utils
from normits_demand.concurrency import multithreading


@dataclasses.dataclass(frozen=True)
class FurnessResults:
    matrix: np.ndarray
    completed_iters: int
    achieved_rmse: float


@dataclasses.dataclass(frozen=True)
class PartialFurnessRequest:
    matrix: np.ndarray
    row_targets: np.ndarray
    col_targets: np.ndarray


class FurnessThreadBase(abc.ABC, multithreading.ReturnOrErrorThread):
    """Base class for running a threaded furness

    Uses its getter_qs to wait for partial matrix inputs. Waits for all
    partial matrices, adds them together, and runs a furness.
    Splits out the furnessed matrix and returns the partial matrices.
    """
    # TODO(BT): Add functionality to allow this to be manually terminated too.

    def __init__(self,
                 area_mats: Dict[Any, np.ndarray],
                 getter_qs: Dict[Any, queue.Queue],
                 putter_qs: Dict[Any, queue.Queue],
                 furness_tol: float,
                 furness_max_iters: int,
                 warning: bool,
                 *args,
                 **kwargs,
                 ):
        """
        Parameters
        ----------
        area_mats:
            A dictionary of boolean matrices indicating where the area of each
            area_id is. Keys are the area_ids.

        getter_qs:
            A dictionary of Queues for each area_id. Queues should pass in
            partial matrices of that area_id. Furness will be run once data
            has been received from all queues.

        putter_qs:
            A dictionary of Queues for each area_id. Queues are used to pass
            furnessed partial matrices on.
            
        furness_tol:
            The maximum difference between the achieved and the target values
            in the furness to tolerate before exiting early. Root mean squared
            area is used to calculate the difference.

        furness_max_iters:
            The maximum number of iterations to complete before exiting
            the furness.

        warning:
            Whether to print a warning or not when the tol cannot be met before
            max_iters.
        """
        multithreading.ReturnOrErrorThread.__init__(self, *args, **kwargs)

        self.getter_qs = getter_qs
        self.putter_qs = putter_qs
        self.area_mats = area_mats

        self.furness_tol = furness_tol
        self.furness_max_iters = furness_max_iters
        self.warning = warning

        self.calib_area_keys = area_mats.keys()

    def _get_q_data(self, need_area_keys: List[int]):
        # TODO(BT): USE multithreading.get_data_from_queue(), but update it
        #  to use lists as well.
        # init
        queue_data = dict.fromkeys(self.calib_area_keys)

        while len(need_area_keys) > 0:

            # Try and get the data
            done_threads = list()
            for area_id in need_area_keys:
                try:
                    data = self.getter_qs[area_id].get(block=False)
                    queue_data[area_id] = data
                    done_threads.append(area_id)
                except queue.Empty:
                    pass

            # Remove ids for data we have
            for item in done_threads:
                need_area_keys.remove(item)

            # Wait for a bit so we don't hammer CPU
            time.sleep(0.1)

        return queue_data

    @abc.abstractmethod
    def get_furness_data(self, need_area_keys: List[Any]):
        """Grabs the needed data for the furness to run

        Parameters
        ----------
        need_area_keys:
            A list of the area keys that we still need to get data for.
            This key can be used in index:
            self.getter_qs
            self.putter_qs
            self.area_mats

        Returns
        -------
        seed_mats:
            A dictionary of retrieved partial seed matrices that need to be
            combined to create the full seed matrix for the furness.

        row_targets:
            The row targets to be used for the furness.
            i.e the target of np.sum(furnessed_matrix, axis=1)

        col_targets:
            The col targets to be used for the furness.
            i.e the target of np.sum(furnessed_matrix, axis=0)
        """
        raise NotImplementedError

    def run_target(self) -> None:
        """Runs a furness once all data received, and passes data back

        Runs forever - therefore needs to be a daemon.
        Overrides parent to run this on thread start.

        Returns
        -------
        None
        """
        # Run until program exit.
        while True:
            # Wait for threads to hand over seed mats
            need_area_keys = list(self.calib_area_keys)

            seed_mats, row_targets, col_targets = self.get_furness_data(need_area_keys)

            # Add all seed mats together
            seed_mat = np.zeros((len(row_targets), len(col_targets)))
            for area_code in self.calib_area_keys:
                seed_mat += seed_mats[area_code]

            # Run the furness
            furnessed_mat, iters, rmse = furness.doubly_constrained_furness(
                seed_vals=seed_mat,
                row_targets=row_targets,
                col_targets=col_targets,
                tol=self.furness_tol,
                max_iters=self.furness_max_iters,
                warning=self.warning,
            )

            # Split the furnessed matrix back out
            furnessed_mats = dict.fromkeys(self.calib_area_keys)
            for area_code in furnessed_mats:
                furnessed_mats[area_code] = furnessed_mat * self.area_mats[area_code]

            # Put the data back on the queues
            for area_id in self.calib_area_keys:
                data = FurnessResults(
                    matrix=furnessed_mats[area_id],
                    completed_iters=iters,
                    achieved_rmse=rmse,
                )
                self.putter_qs[area_id].put(data)


class GravityModelBase(abc.ABC):
    """
    Base Class for gravity models.

    Contains any shared functionality needed across gravity model
    implementations.
    """

    # Class constants
    _avg_cost_col = 'ave_km'        # Should be more generic
    _target_cost_distribution_cols = ['min', 'max', 'trips'] + [_avg_cost_col]
    _least_squares_method = 'trf'

    def __init__(self,
                 row_targets: np.ndarray,
                 col_targets: np.ndarray,
                 cost_function: cost.CostFunction,
                 cost_matrix: np.ndarray,
                 target_cost_distribution: pd.DataFrame,
                 running_log_path: nd.PathLike = None,
                 ):
        # Validate attributes
        target_cost_distribution = pd_utils.reindex_cols(
            target_cost_distribution,
            self._target_cost_distribution_cols,
        )
        
        if running_log_path is not None:
            dir_name, _ = os.path.split(running_log_path)
            if not os.path.exists(dir_name):
                raise FileNotFoundError(
                    "Cannot find the defined directory to write out a"
                    "log. Given the following path: %s"
                    % dir_name
                )

            if os.path.isfile(running_log_path):
                warnings.warn(
                    "Given a log path to a file that already exists. Logs "
                    "will be appended to the end of the file at: %s"
                    % running_log_path
                )

        # Set attributes
        self.row_targets = row_targets
        self.col_targets = col_targets
        self.cost_function = cost_function
        self.cost_matrix = cost_matrix
        self.target_cost_distribution = self._update_tcd(target_cost_distribution)
        self.tcd_bin_edges = self._get_tcd_bin_edges(target_cost_distribution)
        self.running_log_path = running_log_path

        # Running attributes
        self._loop_num = -1
        self._loop_start_time = None
        self._loop_end_time = None
        self._jacobian_mats = None
        self._perceived_factors = None

        # Additional attributes
        self.initial_cost_params = None
        self.initial_convergence = None
        self.optimal_cost_params = None
        self.achieved_band_share = None
        self.achieved_convergence = None
        self.achieved_residuals = None
        self.achieved_distribution = None

    @staticmethod
    def _update_tcd(tcd: pd.DataFrame) -> pd.DataFrame:
        """Extrapolates data where needed"""
        # Add in ave_km where needed
        tcd['ave_km'] = np.where(
            (tcd['ave_km'] == 0) | np.isnan(tcd['ave_km']),
            tcd['min'],
            tcd['ave_km'],
        )

        # Generate the band shares using the given data
        tcd['band_share'] = tcd['trips'].copy()
        tcd['band_share'] /= tcd['band_share'].values.sum()

        return tcd

    @staticmethod
    def _get_tcd_bin_edges(target_cost_distribution: pd.DataFrame) -> List[float]:
        min_bounds = target_cost_distribution['min'].tolist()
        max_bounds = target_cost_distribution['max'].tolist()
        return [min_bounds[0]] + max_bounds

    def _initialise_calibrate_params(self) -> None:
        """Sets running params to their default values for a run"""
        self._loop_num = 1
        self._loop_start_time = timing.current_milli_time()
        self.initial_cost_params = None
        self.initial_convergence = None
        self._perceived_factors = np.ones_like(self.cost_matrix)

    def _cost_params_to_kwargs(self, args: List[Any]) -> Dict[str, Any]:
        """Converts a list or args into kwargs that self.cost_function expects"""
        if len(args) != len(self.cost_function.kw_order):
            raise ValueError(
                "Received the wrong number of args to convert to cost function "
                "kwargs. Expected %s args, but got %s."
                % (len(self.cost_function.kw_order), len(args))
            )

        return {k: v for k, v in zip(self.cost_function.kw_order, args)}

    def _order_cost_params(self, params: Dict[str, Any]) -> List[Any]:
        """Order params into a list that self.cost_function expects"""
        ordered_params = [0] * len(self.cost_function.kw_order)
        for name, value in params.items():
            index = self.cost_function.kw_order.index(name)
            ordered_params[index] = value

        return ordered_params

    def _order_init_params(self, init_params: Dict[str, Any]) -> List[Any]:
        """Order init_params into a list that self.cost_function expects"""
        return self._order_cost_params(init_params)

    def _order_bounds(self) -> Tuple[List[Any], List[Any]]:
        """Order min and max into a tuple of lists that self.cost_function expects"""
        return(
            self._order_cost_params(self.cost_function.param_min),
            self._order_cost_params(self.cost_function.param_max),
        )

    def _cost_distribution(self,
                           matrix: np.ndarray,
                           tcd_bin_edges: List[float],
                           ) -> np.ndarray:
        """Returns the distribution of matrix across self.tcd_bin_edges"""
        return cost_utils.calculate_cost_distribution(
            matrix=matrix,
            cost_matrix=self.cost_matrix,
            bin_edges=tcd_bin_edges,
        )

    def _guess_init_params(self,
                           cost_args: List[float],
                           target_cost_distribution: pd.DataFrame,
                           ):
        """Internal function of _estimate_init_params()

        Guesses what the initial params should be.
        Used by the `optimize.least_squares` function.
        """
        # Convert the cost function args back into kwargs
        cost_kwargs = self._cost_params_to_kwargs(cost_args)

        # Used to optionally increase the cost of long distance trips
        avg_cost_vals = target_cost_distribution[self._avg_cost_col].values

        # Estimate what the cost function will do to the costs - on average
        estimated_cost_vals = self.cost_function.calculate(avg_cost_vals, **cost_kwargs)
        estimated_band_shares = estimated_cost_vals / estimated_cost_vals.sum()

        # return the residuals to the target
        return target_cost_distribution['band_share'].values - estimated_band_shares

    def _estimate_init_params(self,
                              init_params: Dict[str, Any],
                              target_cost_distribution: pd.DataFrame,
                              ):
        """Guesses what the initial params should be.

        Uses the average cost in each band to estimate what changes in
        the cost_params would do to the final cost distributions. This is a
        very coarse grained estimation, but can be used to guess around about
        where the best init params are.
        """
        result = optimize.least_squares(
            fun=self._guess_init_params,
            x0=self._order_init_params(init_params),
            method=self._least_squares_method,
            bounds=self._order_bounds(),
            kwargs={'target_cost_distribution': target_cost_distribution},
        )
        init_params = self._cost_params_to_kwargs(result.x)

        # TODO(BT): standardise this
        if self.cost_function.name == 'LOG_NORMAL':
            init_params['sigma'] *= 0.8
            init_params['mu'] *= 0.5

        return init_params

    def _calculate_perceived_factors(self) -> None:
        """Updates the perceived cost class variables

        Compares the latest run of the gravity model (as defined by the
        variables: self.achieved_band_share)
        and generates a perceived cost factor matrix, which will be applied
        on calls to self._cost_amplify() in the gravity model.

        This function updates the _perceived_factors class variable.
        """
        # Init
        target_band_share = self.target_cost_distribution['band_share'].values

        # Calculate the adjustment per band in target band share.
        # Adjustment is clipped between 0.5 and 2 to limit affect
        perc_factors = np.divide(
            self.achieved_band_share,
            target_band_share,
            where=target_band_share > 0,
            out=np.ones_like(self.achieved_band_share),
        ) ** 0.5
        perc_factors = np.clip(perc_factors, 0.5, 2)

        # Initialise loop
        perc_factors_mat = np.ones_like(self.cost_matrix)
        min_vals = self.target_cost_distribution['min']
        max_vals = self.target_cost_distribution['max']

        # Convert into factors for the cost matrix
        for min_val, max_val, factor in zip(min_vals, max_vals, perc_factors):
            # Get proportion of all trips that are in this band
            distance_mask = (
                (self.cost_matrix >= min_val)
                & (self.cost_matrix < max_val)
            )

            perc_factors_mat = np.multiply(
                perc_factors_mat,
                factor,
                where=distance_mask,
                out=perc_factors_mat,
            )

        # Assign to class attribute
        self._perceived_factors = perc_factors_mat

    def _apply_perceived_factors(self, cost_matrix: np.ndarray) -> np.ndarray:
        return cost_matrix * self._perceived_factors

    def _gravity_function(self,
                          cost_args: List[float],
                          diff_step: float,
                          ):
        """Returns residuals to target cost distribution

        Runs gravity model with given parameters and converts into achieved
        cost distribution. The residuals are then calculated between the
        achieved and the target.

        Used by the `optimize.least_squares` function.

        This function will populate and update:
            self.achieved_band_share
            self.achieved_convergence
            self.achieved_residuals
            self.achieved_distribution
            self.optimal_cost_params
        """
        # Convert the cost function args back into kwargs
        cost_kwargs = self._cost_params_to_kwargs(cost_args)

        # Used to optionally adjust the cost of long distance trips
        cost_matrix = self._apply_perceived_factors(self.cost_matrix)

        # Calculate initial matrix through cost function
        init_matrix = self.cost_function.calculate(cost_matrix, **cost_kwargs)

        # Do some prep for jacobian calculations
        self._jacobian_mats = {'base': init_matrix.copy()}
        for cost_param in self.cost_function.kw_order:
            # Adjust cost slightly
            adj_cost_kwargs = cost_kwargs.copy()
            adj_cost_kwargs[cost_param] += adj_cost_kwargs[cost_param] * diff_step

            # Calculate adjusted cost
            adj_cost = self.cost_function.calculate(cost_matrix, **adj_cost_kwargs)

            self._jacobian_mats[cost_param] = adj_cost

        # Furness trips to trip ends
        matrix, iters, rmse = self.gravity_furness(
            seed_matrix=init_matrix,
            row_targets=self.row_targets,
            col_targets=self.col_targets,
        )

        # Store for the jacobian calculations
        self._jacobian_mats['final'] = matrix.copy()

        # Convert matrix into an achieved distribution curve
        achieved_band_shares = self._cost_distribution(matrix, self.tcd_bin_edges)

        # Evaluate this run
        target_band_shares = self.target_cost_distribution['band_share'].values
        convergence = math_utils.curve_convergence(target_band_shares, achieved_band_shares)
        achieved_residuals = target_band_shares - achieved_band_shares

        # Calculate the time this loop took
        self._loop_end_time = timing.current_milli_time()
        time_taken = self._loop_end_time - self._loop_start_time

        # ## LOG THIS ITERATION ## #
        log_dict = {
            'loop_number': str(self._loop_num),
            'runtime (s)': time_taken / 1000,
        }
        log_dict.update(cost_kwargs)
        log_dict.update({
            'furness_iters': iters,
            'furness_rmse': np.round(rmse, 6),
            'bs_con': np.round(convergence, 4),
        })

        # Append this iteration to log file
        file_ops.safe_dataframe_to_csv(
                pd.DataFrame(log_dict, index=[0]),
                self.running_log_path,
                mode='a',
                header=(not os.path.exists(self.running_log_path)),
                index=False,
        )

        # Update loop params and return the achieved band shares
        self._loop_num += 1
        self._loop_start_time = timing.current_milli_time()
        self._loop_end_time = None

        # Update performance params
        self.achieved_band_share = achieved_band_shares
        self.achieved_convergence = convergence
        self.achieved_residuals = achieved_residuals
        self.achieved_distribution = matrix

        # Store the initial values to log later
        if self.initial_cost_params is None:
            self.initial_cost_params = cost_kwargs
        if self.initial_convergence is None:
            self.initial_convergence = convergence

        return achieved_residuals

    def _jacobian_function(self, cost_args: List[float], diff_step: float):
        """Returns the Jacobian for _gravity_function

        Uses the matrices stored in self._jacobian_mats (which were stored in
        the previous call to self._gravity function) to estimate what a change
        in the cost parameters would do to final furnessed matrix. This is
        then formatted into a Jacobian for optimize.least_squares to use.

        Used by the `optimize.least_squares` function.
        """
        # Initialise the output
        n_bands = len(self.target_cost_distribution['band_share'].values)
        n_cost_params = len(cost_args)
        jacobian = np.zeros((n_bands, n_cost_params))

        # Convert the cost function args back into kwargs
        cost_kwargs = self._cost_params_to_kwargs(cost_args)

        # Estimate what the furness does to the matrix
        furness_factor = np.divide(
            self._jacobian_mats['final'],
            self._jacobian_mats['base'],
            where=self._jacobian_mats['base'] != 0,
            out=np.zeros_like(self._jacobian_mats['base']),
        )

        # Calculate the Jacobian section for each cost param
        for i, cost_param in enumerate(self.cost_function.kw_order):
            # Estimate how the final matrix would be different with a
            # different input cost parameter
            furness_mat = self._jacobian_mats[cost_param] * furness_factor
            adj_weights = furness_mat / furness_mat.sum() if furness_mat.sum() != 0 else 0
            adj_final = self._jacobian_mats['final'].sum() * adj_weights

            # Control to final matrix
            adj_final, iters, rmse = self.jacobian_furness(
                seed_matrix=adj_final,
                row_targets=self._jacobian_mats['final'].sum(axis=1),
                col_targets=self._jacobian_mats['final'].sum(axis=0),
            )

            # Turn into bands
            achieved_band_shares = self._cost_distribution(adj_final, self.tcd_bin_edges)

            # Calculate the Jacobian for this cost param
            jacobian_residuals = self.achieved_band_share - achieved_band_shares
            cost_step = cost_kwargs[cost_param] * diff_step
            cost_jacobian = jacobian_residuals / cost_step

            # Store in the Jacobian
            jacobian[:, i] = cost_jacobian

        return jacobian

    def _calibrate(self,
                   init_params: Dict[str, Any],
                   calibrate_params: bool = True,
                   diff_step: float = 1e-8,
                   ftol: float = 1e-4,
                   xtol: float = 1e-4,
                   grav_max_iters: int = 100,
                   verbose: int = 0,
                   ) -> None:
        """Internal function of calibrate.

        Runs the gravity model, and calibrates the optimal cost parameters
        if calibrate params is set to True. Will do a final run of the
        gravity_function with the optimal parameter found before return.
        """
        # Initialise running params
        self._initialise_calibrate_params()

        # Calculate the optimal cost parameters if we're calibrating
        if calibrate_params is True:
            result = optimize.least_squares(
                fun=self._gravity_function,
                x0=self._order_init_params(init_params),
                method=self._least_squares_method,
                bounds=self._order_bounds(),
                jac=self._jacobian_function,
                verbose=verbose,
                ftol=ftol,
                xtol=xtol,
                max_nfev=grav_max_iters,
                kwargs={'diff_step': diff_step},
            )
            optimal_params = result.x
        else:
            optimal_params = self._order_init_params(init_params)

        # Run an optimal version of the gravity
        self.optimal_cost_params = self._cost_params_to_kwargs(optimal_params)
        self._gravity_function(optimal_params, diff_step=diff_step)

    @abc.abstractmethod
    def gravity_furness(self,
                        seed_matrix: np.ndarray,
                        row_targets: np.ndarray,
                        col_targets: np.ndarray,
                        ) -> Tuple[np.array, int, float]:
        """Runs a doubly constrained furness on the seed matrix

        Wrapper around furness.doubly_constrained_furness, to be used when
        running the furness withing the gravity model.

        Parameters
        ----------
        seed_matrix:
            Initial values for the furness.

        row_targets:
            The target values for the sum of each row.
            i.e np.sum(seed_matrix, axis=1)

        col_targets:
            The target values for the sum of each column
            i.e np.sum(seed_matrix, axis=0)

        Returns
        -------
        furnessed_matrix:
            The final furnessed matrix

        completed_iters:
            The number of completed iterations before exiting

        achieved_rmse:
            The Root Mean Squared Error difference achieved before exiting
        """
        raise NotImplementedError

    @abc.abstractmethod
    def jacobian_furness(self,
                         seed_matrix: np.ndarray,
                         row_targets: np.ndarray,
                         col_targets: np.ndarray,
                         ) -> Tuple[np.array, int, float]:
        """Runs a doubly constrained furness on the seed matrix

        Wrapper around furness.doubly_constrained_furness, to be used when
        running the furness withing the jacobian calculation.

        Parameters
        ----------
        seed_matrix:
            Initial values for the furness.

        row_targets:
            The target values for the sum of each row.
            i.e np.sum(seed_matrix, axis=1)

        col_targets:
            The target values for the sum of each column
            i.e np.sum(seed_matrix, axis=0)

        Returns
        -------
        furnessed_matrix:
            The final furnessed matrix

        completed_iters:
            The number of completed iterations before exiting

        achieved_rmse:
            The Root Mean Squared Error difference achieved before exiting
        """
        raise NotImplementedError


class GravityModelCalibrator(GravityModelBase):
    # TODO(BT): Write GravityModelCalibrator docs

    def __init__(self,
                 row_targets: np.ndarray,
                 col_targets: np.ndarray,
                 cost_function: cost.CostFunction,
                 cost_matrix: np.ndarray,
                 target_cost_distribution: pd.DataFrame,
                 target_convergence: float,
                 furness_max_iters: int,
                 furness_tol: float,
                 use_perceived_factors: bool = True,
                 running_log_path: nd.PathLike = None,
                 ):
        # TODO(BT): Write GravityModelCalibrator __init__ docs
        super().__init__(
            cost_function=cost_function,
            cost_matrix=cost_matrix,
            target_cost_distribution=target_cost_distribution,
            running_log_path=running_log_path,
            row_targets=row_targets,
            col_targets=col_targets,
        )

        # Set attributes
        self.row_targets = row_targets
        self.col_targets = col_targets
        self.target_cost_distribution = self._update_tcd(target_cost_distribution)
        self.tcd_bin_edges = self._get_tcd_bin_edges(target_cost_distribution)
        self.furness_max_iters = furness_max_iters
        self.furness_tol = furness_tol
        self.use_perceived_factors = use_perceived_factors
        self.running_log_path = running_log_path

        self.target_convergence = target_convergence

    def gravity_furness(self,
                        seed_matrix: np.ndarray,
                        row_targets: np.ndarray,
                        col_targets: np.ndarray,
                        ) -> Tuple[np.array, int, float]:
        """Runs a doubly constrained furness on the seed matrix

        Wrapper around furness.doubly_constrained_furness, using class
        attributes to set up the function call.

        Parameters
        ----------
        seed_matrix:
            Initial values for the furness.

        row_targets:
            The target values for the sum of each row.
            i.e np.sum(seed_matrix, axis=1)

        col_targets:
            The target values for the sum of each column
            i.e np.sum(seed_matrix, axis=0)

        Returns
        -------
        furnessed_matrix:
            The final furnessed matrix

        completed_iters:
            The number of completed iterations before exiting

        achieved_rmse:
            The Root Mean Squared Error difference achieved before exiting
        """
        return furness.doubly_constrained_furness(
            seed_vals=seed_matrix,
            row_targets=row_targets,
            col_targets=col_targets,
            tol=self.furness_tol,
            max_iters=self.furness_max_iters,
        )

    def jacobian_furness(self,
                         seed_matrix: np.ndarray,
                         row_targets: np.ndarray,
                         col_targets: np.ndarray,
                         ) -> Tuple[np.array, int, float]:
        """Runs a doubly constrained furness on the seed matrix

        Wrapper around furness.doubly_constrained_furness, to be used when
        running the furness withing the jacobian calculation.

        Parameters
        ----------
        seed_matrix:
            Initial values for the furness.

        row_targets:
            The target values for the sum of each row.
            i.e np.sum(seed_matrix, axis=1)

        col_targets:
            The target values for the sum of each column
            i.e np.sum(seed_matrix, axis=0)

        Returns
        -------
        furnessed_matrix:
            The final furnessed matrix

        completed_iters:
            The number of completed iterations before exiting

        achieved_rmse:
            The Root Mean Squared Error difference achieved before exiting
        """
        return furness.doubly_constrained_furness(
            seed_vals=seed_matrix,
            row_targets=row_targets,
            col_targets=col_targets,
            tol=1e-6,
            max_iters=20,
            warning=False,
        )

    def calibrate(self,
                  init_params: Dict[str, Any],
                  estimate_init_params: bool = False,
                  calibrate_params: bool = True,
                  diff_step: float = 1e-8,
                  ftol: float = 1e-4,
                  xtol: float = 1e-4,
                  grav_max_iters: int = 100,
                  verbose: int = 0,
                  ):
        """Finds the optimal parameters for self.cost_function

        Optimal parameters are found using `scipy.optimize.least_squares`
        to fit the distributed row/col targets to self.target_tld. Once
        the optimal parameters are found, the gravity model is run one last
        time to check the self.target_convergence has been met. This also
        populates a number of attributes with values from the optimal run:
        self.achieved_band_share
        self.achieved_convergence
        self.achieved_residuals
        self.achieved_distribution

        Parameters
        ----------
        init_params:
            A dictionary of {parameter_name: parameter_value} to pass
            into the cost function as initial parameters.

        estimate_init_params:
            Whether to ignore the given init_params and estimate new ones
            using least squares, or just use the given init_params to start
            with.

        calibrate_params:
            Whether to calibrate the cost parameters or not. If not
            calibrating, the given init_params will be assumed to be
            optimal.

        diff_step:
            Copied from scipy.optimize.least_squares documentation, where it
            is passed to:
            Determines the relative step size for the finite difference
            approximation of the Jacobian. The actual step is computed as
            x * diff_step. If None (default), then diff_step is taken to be a
            conventional “optimal” power of machine epsilon for the finite
            difference scheme used

        ftol:
            The tolerance to pass to scipy.optimize.least_squares. The search
            will stop once this tolerance has been met. This is the
            tolerance for termination by the change of the cost function

        xtol:
            The tolerance to pass to scipy.optimize.least_squares. The search
            will stop once this tolerance has been met. This is the
            tolerance for termination by the change of the independent
            variables.

        grav_max_iters:
            The maximum number of calibration iterations to complete before
            termination if the ftol has not been met.

        verbose:
            Copied from scipy.optimize.least_squares documentation, where it
            is passed to:
            Level of algorithm’s verbosity:
            - 0 (default) : work silently.
            - 1 : display a termination report.
            - 2 : display progress during iterations (not supported by ‘lm’ method).

        Returns
        -------
        optimal_cost_params:
            Returns a dictionary of the same shape as init_params. The values
            will be the optimal cost parameters to get the best band share
            convergence.

        Raises
        ------
        ValueError
            If the generated trip matrix contains any
            non-finite values.

        See Also
        --------
        gravity_model
        scipy.optimize.least_squares
        """
        # Validate init_params
        self.cost_function.validate_params(init_params)

        # Estimate what the initial params should be
        if estimate_init_params:
            init_params = self._estimate_init_params(
                init_params=init_params,
                target_cost_distribution=self.target_cost_distribution,
            )

        # Figure out the optimal cost params
        self._calibrate(
            init_params=init_params,
            calibrate_params=calibrate_params,
            diff_step=diff_step,
            ftol=ftol,
            xtol=xtol,
            grav_max_iters=grav_max_iters,
            verbose=verbose,

        )

        # Just return if not using perceived factors
        if not self.use_perceived_factors:
            return self.optimal_cost_params

        # ## APPLY PERCEIVED FACTORS IF WE CAN ## #
        upper_limit = self.target_convergence + 0.03
        lower_limit = self.target_convergence - 0.15

        # Just return if upper limit has been beaten
        if self.achieved_convergence > upper_limit:
            return self.optimal_cost_params

        # Warn if the lower limit hasn't been reached
        if self.achieved_convergence < lower_limit:
            warnings.warn(
                "Calibration was not able to reach the lower threshold "
                "required to use perceived factors.\n"
                "Target convergence: %s\n"
                "Upper Limit: %s\n"
                "Achieved convergence: %s"
                % (self.target_convergence, upper_limit, self.achieved_convergence)
            )
            return self.optimal_cost_params

        # If here, it's safe to use perceived factors
        self._calculate_perceived_factors()

        # Calibrate again, using the perceived factors
        self._calibrate(
            init_params=self.optimal_cost_params.copy(),
            calibrate_params=calibrate_params,
            diff_step=diff_step,
            ftol=ftol,
            xtol=xtol,
            grav_max_iters=grav_max_iters,
            verbose=verbose,
        )

        if self.achieved_convergence < self.target_convergence:
            warnings.warn(
                "Calibration with perceived factors was not able to reach the "
                "target_convergence.\n"
                "Target convergence: %s\n"
                "Achieved convergence: %s"
                % (self.target_convergence, self.achieved_convergence)
            )

        return self.optimal_cost_params


class GravityFurnessThread(FurnessThreadBase):
    """Collects partial matrices and runs a furness

    Uses its getter_qs to wait for partial matrix inputs. Waits for all
    partial matrices, adds them together, and runs a furness.
    Splits out the furnessed matrix and returns the partial matrices.
    """
    def __init__(self,
                 row_targets: np.ndarray,
                 col_targets: np.ndarray,
                 *args,
                 **kwargs,
                 ):
        """
        Parameters
        ----------
        row_targets:
            The row targets to aim for when running the furness. This should
            be the target when `.sum(axis=1) is applied to the full matrix.

        col_targets:
            The row targets to aim for when running the furness. This should
            be the target when `.sum(axis=1) is applied to the full matrix.

        *args, **kwargs:
            Arguments to be passed to parent FurnessThreadBase class

        See Also
        --------
        `FurnessThreadBase`
        """
        super().__init__(
            *args,
            **kwargs,
        )

        # Set attributes
        self.row_targets = row_targets
        self.col_targets = col_targets

    def get_furness_data(self, need_area_keys: List[Any]):
        """Grabs the needed data for the furness to run

        Parameters
        ----------
        need_area_keys:
            A list of the area keys that we still need to get data for.
            This key can be used in index:
            self.getter_qs
            self.putter_qs
            self.area_mats

        Returns
        -------
        seed_mats:
            A dictionary of retrieved partial seed matrices that need to be
            combined to create the full seed matrix for the furness.

        row_targets:
            The row targets to be used for the furness.
            i.e the target of np.sum(furnessed_matrix, axis=1)

        col_targets:
            The col targets to be used for the furness.
            i.e the target of np.sum(furnessed_matrix, axis=0)
        """
        return (
            self._get_q_data(need_area_keys),
            self.row_targets,
            self.col_targets,
        )


class JacobianFurnessThread(FurnessThreadBase):
    """Collects partial matrices and runs a furness

    Uses its getter_qs to wait for partial matrix inputs. Waits for all
    partial matrices, adds them together, and runs a furness.
    Splits out the furnessed matrix and returns the partial matrices.
    """
    def get_furness_data(self, need_area_keys: List[Any]):
        """Grabs the needed data for the furness to run

        Parameters
        ----------
        need_area_keys:
            A list of the area keys that we still need to get data for.
            This key can be used in index:
            self.getter_qs
            self.putter_qs
            self.area_mats

        Returns
        -------
        seed_mats:
            A dictionary of retrieved partial seed matrices that need to be
            combined to create the full seed matrix for the furness.

        row_targets:
            The row targets to be used for the furness.
            i.e the target of np.sum(furnessed_matrix, axis=1)

        col_targets:
            The col targets to be used for the furness.
            i.e the target of np.sum(furnessed_matrix, axis=0)
        """
        # Get all the data
        partial_furness_requests = self._get_q_data(need_area_keys)

        # Split out items
        seed_mats = dict()
        row_targets_list = list()
        col_targets_list = list()
        for key, request in partial_furness_requests.items():
            seed_mats[key] = request.matrix
            row_targets_list.append(request.row_targets)
            col_targets_list.append(request.col_targets)

        # Combine individual items
        row_targets = functools.reduce(operator.add, row_targets_list)
        col_targets = functools.reduce(operator.add, col_targets_list)

        return seed_mats, row_targets, col_targets


class SingleTLDCalibratorThread(multithreading.ReturnOrErrorThread, GravityModelBase):
    """Calibrate Gravity Model params for a single TLD

    Used internally in MultiTLDGravityModelCalibrator. Each TLD is split out
    with its data, then handed over to one of these threads to find the
    optimal params alongside one another.
    """
    def __init__(self,
                 cost_function: cost.CostFunction,
                 cost_matrix: np.ndarray,
                 target_cost_distribution: pd.DataFrame,
                 target_convergence: float,
                 init_params: Dict[str, Any],
                 gravity_putter_q: queue.Queue,
                 gravity_getter_q: queue.Queue,
                 jacobian_putter_q: queue.Queue,
                 jacobian_getter_q: queue.Queue,
                 use_perceived_factors: bool = True,
                 estimate_init_params: bool = False,
                 running_log_path: nd.PathLike = None,
                 calibrate_params: bool = True,
                 diff_step: float = 1e-8,
                 ftol: float = 1e-4,
                 xtol: float = 1e-4,
                 grav_max_iters: int = 100,
                 verbose: int = 0,
                 thread_name: str = None,
                 *args,
                 **kwargs,
                 ):
        # Call parent classes
        multithreading.ReturnOrErrorThread.__init__(
            self,
            name=thread_name,
            *args,
            **kwargs,
        )
        # row and col targets aren't used in this implementation. See
        # self.gravity_furness for more info.
        GravityModelBase.__init__(
            self,
            row_targets=None,
            col_targets=None,
            cost_function=cost_function,
            cost_matrix=cost_matrix,
            target_cost_distribution=target_cost_distribution,
            running_log_path=running_log_path,
        )

        # Assign other attributes
        self.target_convergence = target_convergence
        self.init_params = init_params
        self.use_perceived_factors = use_perceived_factors
        self.estimate_init_params = estimate_init_params

        # optimize_params
        self.calibrate_params = calibrate_params
        self.diff_step = diff_step
        self.ftol = ftol
        self.xtol = xtol
        self.grav_max_iters = grav_max_iters
        self.verbose = verbose

        # Threading attributes
        self.gravity_putter_q = gravity_putter_q
        self.gravity_getter_q = gravity_getter_q
        self.jacobian_putter_q = jacobian_putter_q
        self.jacobian_getter_q = jacobian_getter_q

    def run_target(self) -> Dict[str, Any]:
        """Finds the optimal parameters for self.cost_function

        Optimal parameters are found using `scipy.optimize.least_squares`
        to fit the distributed row/col targets to
        self.target_cost_distribution. Once the optimal parameters are found,
        the gravity model is run one last time to check the
        self.target_convergence has been met.
        Overrides parent to run this on thread start.

        Returns
        -------
        optimal_params:
            Returns a dictionary of the same shape as self.init_params. The
            values will be the optimal cost parameters to get the best band
            share convergence for self.target_cost_distribution
        """
        # Validate init_params
        self.cost_function.validate_params(self.init_params)

        # Estimate what the initial params should be
        if self.estimate_init_params:
            init_params = self._estimate_init_params(
                init_params=self.init_params,
                target_cost_distribution=self.target_cost_distribution,
            )
        else:
            init_params = self.init_params

        # Figure out the optimal cost params
        self._calibrate(
            init_params=init_params,
            calibrate_params=self.calibrate_params,
            diff_step=self.diff_step,
            ftol=self.ftol,
            xtol=self.xtol,
            grav_max_iters=self.grav_max_iters,
            verbose=self.verbose,
        )
        return

    def gravity_furness(self,
                        seed_matrix: np.ndarray,
                        row_targets: np.ndarray,
                        col_targets: np.ndarray,
                        ) -> Tuple[np.array, int, float]:
        """Runs a doubly constrained furness on the seed matrix

        Wrapper around furness.doubly_constrained_furness, using class
        attributes to set up the function call.

        Parameters
        ----------
        seed_matrix:
            Initial values for the furness.

        row_targets:
            The target values for the sum of each row.
            i.e np.sum(seed_matrix, axis=1)

        col_targets:
            The target values for the sum of each column
            i.e np.sum(seed_matrix, axis=0)

        Returns
        -------
        furnessed_matrix:
            The final furnessed matrix

        completed_iters:
            The number of completed iterations before exiting

        achieved_rmse:
            The Root Mean Squared Error difference achieved before exiting
        """
        self.gravity_putter_q.put(seed_matrix)
        furness_data = multithreading.get_data_from_queue(self.gravity_getter_q)
        return (
            furness_data.matrix,
            furness_data.completed_iters,
            furness_data.achieved_rmse,
        )

    def jacobian_furness(self,
                         seed_matrix: np.ndarray,
                         row_targets: np.ndarray,
                         col_targets: np.ndarray,
                         ) -> Tuple[np.array, int, float]:
        """Runs a doubly constrained furness on the seed matrix

        Wrapper around furness.doubly_constrained_furness, to be used when
        running the furness withing the jacobian calculation.

        Parameters
        ----------
        seed_matrix:
            Initial values for the furness.

        row_targets:
            The target values for the sum of each row.
            i.e np.sum(matrix, axis=1)

        col_targets:
            The target values for the sum of each column
            i.e np.sum(matrix, axis=0)

        Returns
        -------
        furnessed_matrix:
            The final furnessed matrix

        completed_iters:
            The number of completed iterations before exiting

        achieved_rmse:
            The Root Mean Squared Error difference achieved before exiting
        """
        # Create a request and place on the queue
        request = PartialFurnessRequest(
            matrix=seed_matrix,
            row_targets=row_targets,
            col_targets=col_targets,
        )

        self.jacobian_putter_q.put(request)

        # Get the return data
        furness_data = multithreading.get_data_from_queue(self.jacobian_getter_q)
        return (
            furness_data.matrix,
            furness_data.completed_iters,
            furness_data.achieved_rmse,
        )


class MultiTLDGravityModelCalibrator:
    # TODO(BT): Write MultiTLDGravityModelCalibrator docs

    _ignore_calib_area_value = -1

    def __init__(self,
                 row_targets: np.ndarray,
                 col_targets: np.ndarray,
                 calibration_matrix: np.ndarray,
                 cost_function: cost.CostFunction,
                 cost_matrix: np.ndarray,
                 target_cost_distributions: Dict[Any, pd.DataFrame],
                 calibration_naming: Dict[Any, Any],
                 target_convergence: float,
                 furness_max_iters: int,
                 furness_tol: float,
                 use_perceived_factors: bool = True,
                 running_log_path: nd.PathLike = None,
                 ):
        # TODO(BT): Write MultiTLDGravityModelCalibrator __init__ docs
        # Set up logging
        if running_log_path is not None:
            dir_name, _ = os.path.split(running_log_path)
            if not os.path.exists(dir_name):
                raise FileNotFoundError(
                    "Cannot find the defined directory to write out a"
                    "log. Given the following path: %s"
                    % dir_name
                )

            if os.path.isfile(running_log_path):
                warnings.warn(
                    "Given a log path to a file that already exists. Logs "
                    "will be appended to the end of the file at: %s"
                    % running_log_path
                )

        # Set attributes
        self.row_targets = row_targets
        self.col_targets = col_targets
        self.cost_function = cost_function
        self.cost_matrix = cost_matrix
        self.furness_max_iters = furness_max_iters
        self.furness_tol = furness_tol
        self.use_perceived_factors = use_perceived_factors
        self.running_log_path = running_log_path

        self.target_convergence = target_convergence

        # Ensure the calibration stuff was passed in correctly
        self.calibration_matrix = calibration_matrix
        calib_areas = list(np.unique(calibration_matrix))
        self.calib_areas = du.list_safe_remove(calib_areas, [self._ignore_calib_area_value])

        if not checks.all_keys_exist(calibration_naming, self.calib_areas):
            raise ValueError(
                "Calibration matrix needs to calibrate on %s\n"
                "However, names were only given for %s"
                % (self.calib_areas, calibration_naming.keys())
            )

        if not checks.all_keys_exist(target_cost_distributions, self.calib_areas):
            raise ValueError(
                "Calibration matrix needs to calibrate on %s\n"
                "However, target_cost_distributions were only given for %s"
                % (self.calib_areas, calibration_naming.keys())
            )

        self.calibration_naming = calibration_naming
        self.target_cost_distributions = target_cost_distributions

        # Running attributes
        # self._loop_num = -1
        # self._loop_start_time = None
        # self._loop_end_time = None
        # self._jacobian_mats = None
        # self._perceived_factors = None
        #
        # # Additional attributes
        # self.initial_cost_params = None
        # self.initial_convergence = None
        # self.optimal_cost_params = None
        # self.achieved_band_share = None
        # self.achieved_convergence = None
        # self.achieved_residuals = None
        # self.achieved_distribution = None

    def _threaded_calibrate(self,
                            init_params: Dict[str, Any],
                            estimate_init_params: bool,
                            calibrate_params: bool,
                            diff_step: float = 1e-8,
                            ftol: float = 1e-4,
                            xtol: float = 1e-4,
                            grav_max_iters: int = 100,
                            verbose: int = 0,
                            ) -> Dict[Any, Dict[str, Any]]:
        """Core of the calibration method - nested inside _calibrate

        Returns
        -------
        optimal_params:
            A dictionary of optimal parameters for each calibration area.
        """
        # Init
        optimal_params = dict.fromkeys(self.calib_areas)

        # Set up the queues for threads to communicate
        area_mats = dict.fromkeys(self.calib_areas)
        gravity_putter_qs = dict.fromkeys(self.calib_areas)
        gravity_getter_qs = dict.fromkeys(self.calib_areas)
        jacobian_putter_qs = dict.fromkeys(self.calib_areas)
        jacobian_getter_qs = dict.fromkeys(self.calib_areas)
        for area_id in self.calib_areas:
            area_mats[area_id] = self.calibration_matrix == area_id
            gravity_putter_qs[area_id] = queue.Queue(1)
            gravity_getter_qs[area_id] = queue.Queue(1)
            jacobian_putter_qs[area_id] = queue.Queue(1)
            jacobian_getter_qs[area_id] = queue.Queue(1)

        # Initialise the central gravity furness thread
        gravity_furness_thread = GravityFurnessThread(
            daemon=True,
            row_targets=self.row_targets,
            col_targets=self.col_targets,
            getter_qs=gravity_putter_qs,
            putter_qs=gravity_getter_qs,
            area_mats=area_mats,
            furness_tol=self.furness_tol,
            furness_max_iters=self.furness_max_iters,
            warning=True,
        )
        gravity_furness_thread.start()

        # Initialise the central jacobian furness thread
        jacobian_furness_thread = JacobianFurnessThread(
            daemon=True,
            getter_qs=jacobian_putter_qs,
            putter_qs=jacobian_getter_qs,
            area_mats=area_mats,
            furness_tol=1e-6,
            furness_max_iters=50,
            warning=False,
        )
        jacobian_furness_thread.start()

        # Start the gravity processes
        calibrator_threads = dict.fromkeys(self.calib_areas)
        for area_id in self.calib_areas:
            # Get just the costs for this area
            area_cost = self.cost_matrix * area_mats[area_id]

            # Set up where to put the logs
            dir_name, fname = os.path.split(self.running_log_path)
            area_dir_name = os.path.join(dir_name, self.calibration_naming[area_id])
            file_ops.create_folder(area_dir_name)
            area_running_log_path = os.path.join(area_dir_name, fname)

            # Start a thread to calibrate each area
            calibrator_threads[area_id] = SingleTLDCalibratorThread(
                thread_name=self.calibration_naming[area_id],
                cost_function=self.cost_function,
                cost_matrix=area_cost,
                init_params=init_params,
                estimate_init_params=estimate_init_params,
                target_cost_distribution=self.target_cost_distributions[area_id],
                target_convergence=self.target_convergence,
                running_log_path=area_running_log_path,
                gravity_putter_q=gravity_putter_qs[area_id],
                gravity_getter_q=gravity_getter_qs[area_id],
                jacobian_putter_q=jacobian_putter_qs[area_id],
                jacobian_getter_q=jacobian_getter_qs[area_id],
                calibrate_params=calibrate_params,
                diff_step=diff_step,
                ftol=ftol,
                xtol=xtol,
                grav_max_iters=grav_max_iters,
                verbose=verbose,
            )
            calibrator_threads[area_id].start()

        furnessed_mats = multithreading.wait_for_thread_dict_return_or_error(
            threads=calibrator_threads,
            pbar_kwargs={'disable': False}
        )

        print(self.calibration_matrix.sum())
        print(self.target_cost_distributions)
        print(self.calibration_naming)
        print("Made it!")
        exit()

    def _multi_tld_calibrate(self,
                             init_params: Dict[str, Any],
                             estimate_init_params: bool,
                             calibrate_params: bool = True,
                             diff_step: float = 1e-8,
                             ftol: float = 1e-4,
                             xtol: float = 1e-4,
                             grav_max_iters: int = 100,
                             verbose: int = 0,
                             ):
        """Internal function of calibrate.

        Runs multiple gravity models, one for each area_id, and calibrates
        the optimal cost parameters if calibrate params is set to True.


        This function will populate and update:
        self.achieved_band_share
        self.achieved_convergence
        self.achieved_residuals
        self.achieved_distribution
        self.optimal_cost_params
        """
        # Calculate the optimal cost parameters if we're calibrating
        if calibrate_params is True:
            optimal_params = self._threaded_calibrate(
                init_params=init_params,
                estimate_init_params=estimate_init_params,
                calibrate_params=calibrate_params,
                diff_step=diff_step,
                ftol=ftol,
                xtol=xtol,
                grav_max_iters=grav_max_iters,
                verbose=verbose,
            )
        else:
            optimal_params = dict.fromkeys(self.calib_areas)
            for area_id in self.calib_areas:
                optimal_params[area_id] = init_params

        # TODO(BT): Pull the best param data from each thread?
        #  Logs the best params at the end!

    def calibrate(self,
                  init_params: Dict[str, Any],
                  estimate_init_params: bool = False,
                  calibrate_params: bool = True,
                  diff_step: float = 1e-8,
                  ftol: float = 1e-4,
                  xtol: float = 1e-4,
                  grav_max_iters: int = 100,
                  verbose: int = 0,
                  ):
        # TODO(BT): WRITE DOCS!
        # Validate init_params
        self.cost_function.validate_params(init_params)

        # Figure out the optimal cost params
        self._multi_tld_calibrate(
            init_params=init_params,
            estimate_init_params=estimate_init_params,
            calibrate_params=calibrate_params,
            diff_step=diff_step,
            ftol=ftol,
            xtol=xtol,
            grav_max_iters=grav_max_iters,
            verbose=verbose,
        )


def gravity_model(row_targets: np.ndarray,
                  col_targets: np.ndarray,
                  cost_function: cost.CostFunction,
                  costs: np.ndarray,
                  furness_max_iters: int,
                  furness_tol: float,
                  **cost_params
                  ):
    """
    Runs a gravity model and returns the distributed row/col targets

    Uses the given cost function to generate an initial matrix which is
    used in a double constrained furness to distribute the row and column
    targets across a matrix. The cost_params can be used to achieve different
    results based on the cost function.

    Parameters
    ----------
    row_targets:
        The targets for the rows to sum to. These are usually Productions
        in Trip Ends.

    col_targets:
        The targets for the columns to sum to. These are usually Attractions
        in Trip Ends.

    cost_function:
        A cost function class defining how to calculate the seed matrix based
        on the given cost. cost_params will be passed directly into this
        function.

    costs:
        A matrix of the base costs to use. This will be passed into
        cost_function alongside cost_params. Usually this will need to be
        the same shape as (len(row_targets), len(col_targets)).

    furness_max_iters:
        The maximum number of iterations for the furness to complete before
        giving up and outputting what it has managed to achieve.

    furness_tol:
        The R2 difference to try and achieve between the row/col targets
        and the generated matrix. The smaller the tolerance the closer to the
        targets the return matrix will be.

    cost_params:
        Any additional parameters that should be passed through to the cost
        function.

    Returns
    -------
    distributed_matrix:
        A matrix of the row/col targets distributed into a matrix of shape
        (len(row_targets), len(col_targets))

    completed_iters:
        The number of iterations completed by the doubly constrained furness
        before exiting

    achieved_rmse:
        The Root Mean Squared Error achieved by the doubly constrained furness
        before exiting

    Raises
    ------
    TypeError:
        If some of the cost_params are not valid cost parameters, or not all
        cost parameters have been given.
    """
    # Validate additional arguments passed in
    equal, extra, missing = du.compare_sets(
        set(cost_params.keys()),
        set(cost_function.param_names),
    )

    if not equal:
        raise TypeError(
            "gravity_model() got one or more unexpected keyword arguments.\n"
            "Received the following extra arguments: %s\n"
            "While missing arguments: %s"
            % (extra, missing)
        )

    # Calculate initial matrix through cost function
    init_matrix = cost_function.calculate(costs, **cost_params)

    # Furness trips to trip ends
    matrix, iters, rmse = furness.doubly_constrained_furness(
        seed_vals=init_matrix,
        row_targets=row_targets,
        col_targets=col_targets,
        tol=furness_tol,
        max_iters=furness_max_iters,
    )

    return matrix, iters, rmse

