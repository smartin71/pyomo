#  ___________________________________________________________________________
#
#  Pyomo: Python Optimization Modeling Objects
#  Copyright 2017 National Technology and Engineering Solutions of Sandia, LLC
#  Under the terms of Contract DE-NA0003525 with National Technology and 
#  Engineering Solutions of Sandia, LLC, the U.S. Government retains certain 
#  rights in this software.
#  This software is distributed under the 3-clause BSD License.
#  ___________________________________________________________________________
from pyomo.contrib.gdpopt.initialize_subproblems import (
    initialize_master_problem, get_subproblem, add_util_block, 
    add_disjunct_list, add_variable_list, add_constraint_list, 
    save_initial_values)
from pyomo.contrib.gdpopt.mip_solve import solve_linear_GDP
from pyomo.contrib.gdpopt.util import (time_code, lower_logger_level_to)
from pyomo.contrib.gdpopt.algorithm_base_class import _GDPoptAlgorithm
from pyomo.contrib.gdpopt.config_options import (
    _add_OA_configs, _add_mip_solver_configs, _add_nlp_solver_configs, 
    _add_tolerance_configs)

from pyomo.core import (Constraint, Block, Objective, minimize, Expression, Var)
from pyomo.opt.base import SolverFactory
from pyomo.gdp import Disjunct

## DEBUG 
from nose.tools import set_trace

@SolverFactory.register(
    '_logic_based_oa',
    doc='GDP Logic-Based Outer Approximation (LOA) solver')
class GDP_LOA_Solver(_GDPoptAlgorithm):
    CONFIG = _GDPoptAlgorithm.CONFIG
    _add_OA_configs(CONFIG)
    _add_mip_solver_configs(CONFIG)
    _add_nlp_solver_configs(CONFIG)
    _add_tolerance_configs(CONFIG)

    def __init__(self, **kwds):
        self.CONFIG = self.CONFIG(kwds)
        super(GDP_LOA_Solver, self).__init__()

    def solve(self, model, **kwds):
        config = self.CONFIG(kwds.pop('options', {}), preserve_implicit=True)
        config.set_value(kwds)
        
        super().solve(model, config)
        min_logging_level = logging.INFO if config.tee else None
        with time_code(self.timing, 'total', is_main_timer=True), \
            lower_logger_level_to(config.logger, min_logging_level):
            return self._solve_gdp_with_loa(model, config)

    def _solve_gdp_with_loa(self, original_model, config):
        logger = config.logger

        # Make a block that we will store some component lists on so that after
        # we clone we know who's who
        util_block = add_util_block(original_model)
        # Needed for finding indicator_vars mainly
        add_disjunct_list(util_block)
        # To transfer solutions between MILP and NLP
        add_variable_list(util_block)
        # We'll need these to get dual info after solving subproblems
        add_constraint_list(util_block)

        # create model to hold the subproblems: We create this first because
        # certain initialization strategies for the master problem need it.
        subproblem = get_subproblem(original_model)
        # TODO: use getname and a bufffer!
        subproblem_util_block = subproblem.component(util_block.name)
        save_initial_values(subproblem_util_block)

        # create master MILP
        master_util_block = initialize_master_problem(util_block,
                                                      subproblem_util_block,
                                                      config, self)

        # main loop
        while self.master_iteration < config.iterlim:
            # Set iteration counters for new master iteration.
            self.master_iteration += 1
            self.mip_iteration = 0
            self.nlp_iteration = 0

            # print line for visual display
            logger.info('---GDPopt Master Iteration %s---' % 
                        self.master_iteration)

            # solve linear master problem
            with time_code(self.timing, 'mip'):
                (oa_obj, 
                 original_obj) = self._setup_augmented_Lagrangian_objective(
                     master_util_block, config)
                mip_feasible = solve_linear_GDP(master_util_block, config,
                                                self.timing)
                self._update_after_master_problem_solve(mip_feasible, oa_obj,
                                                        logger)
                # TODO: No idea what args these callbacks should actually take
                config.call_after_master_solve(master_util_block, self)

            # Check termination conditions
            if self.any_termination_criterion_met(config):
                break

            with time_code(self.timing, 'nlp'):
                # TODO ESJ: You are finally here! Time to untangle the
                # subproblem solves...
                nlp_result = solve_local_subproblem(mip_result, solve_info,
                                                    config)
            if nlp_result.feasible:
                add_outer_approximation_cuts(nlp_result, solve_info, config)

            # Add integer cut
            add_integer_cut( mip_result.var_values, solve_info.linear_GDP,
                             solve_info, config, feasible=nlp_result.feasible)

            # Check termination conditions
            if any_termination_criterion_met(solve_info, config):
                break

        return results

    def _setup_augmented_Lagrangian_objective(self, master_util_block, config):
        m = master_util_block.model()
        self.mip_iteration += 1
        main_objective = next(m.component_data_objects(Objective, active=True))

        # Set up augmented Lagrangean penalty objective
        main_objective.deactivate()
        sign_adjust = 1 if main_objective.sense == minimize else -1
        master_util_block.OA_penalty_expr = Expression(
            expr=sign_adjust * config.OA_penalty_factor *
            sum(v for v in m.component_data_objects(
                ctype=Var, descend_into=(Block, Disjunct))
                if v.parent_component().local_name == 'GDPopt_OA_slacks'))
        master_util_block.oa_obj = Objective(
            expr=main_objective.expr + master_util_block.OA_penalty_expr,
            sense=main_objective.sense)

        return (master_util_block.oa_obj.expr, main_objective.expr)
