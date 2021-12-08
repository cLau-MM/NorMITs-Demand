# -*- coding: utf-8 -*-
"""
Created on Mon Jun 29 16:43:09 2020
"""
import os

import normits_demand.build.pathing as pathing
import normits_demand.utils.utils as nup


class TMSPathing(pathing.Pathing):

    def __init__(
            self,
            config_path,
            param_file):
        super().__init__(config_path,
                         param_file)

        # Set top level model folder
        model_path = os.path.join(
            self.run_folder,
            self.params['iteration'])

        # Set production path
        production_path = os.path.join(
            model_path,
            'Production Outputs')

        # Set attraction path
        attraction_path = os.path.join(
            model_path,
            'Attraction Outputs')

        # Production import path
        hb_p_import_path = os.path.join(
                production_path,
                'hb_productions_' +
                self.params['model_zoning'].lower() +
                '.csv')
        hb_a_import_path = os.path.join(
                attraction_path,
                'hb_attractions_' +
                self.params['model_zoning'].lower() +
                '.csv')

        nhb_p_import_path = os.path.join(
                production_path,
                'nhb_productions_' +
                self.params['model_zoning'].lower() +
                '.csv')
        nhb_a_import_path = os.path.join(
                attraction_path,
                'nhb_attractions_' +
                self.params['model_zoning'].lower() +
                '.csv')

        # Create project folders
        distribution_path = os.path.join(
            model_path,
            'Distribution Outputs')
        nup.create_folder(distribution_path, chDir=False)

        reports = os.path.join(
            distribution_path,
            'Logs & Reports')
        nup.create_folder(reports, chDir=False)

        external_export = os.path.join(
            distribution_path,
            '/External Distributions')
        nup.create_folder(external_export, chDir=False)

        non_dist_out = os.path.join(
            distribution_path,
            '/24hr Non Dist Matrices')
        nup.create_folder(non_dist_out, chDir=False)

        # Compile into import and export
        self.tms_in = {
            'hb_p': hb_p_import_path,
            'hb_a': hb_a_import_path,
            'nhb_p': nhb_p_import_path,
            'nhb_a': nhb_a_import_path}

        self.tms_out = {
            'p': production_path,
            'a': attraction_path,
            'external': external_export,
            'non_dist': non_dist_out,
            'reports': reports}

        # BACKLOG: Check for init params or else make them

    # BACKLOG: PA to OD handling here?