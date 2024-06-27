"""Converts NHB from avg weekday to avg hour
"""
import os
import sys
import time

import pandas as pd
import numpy as np
from collections import defaultdict

root_folder = r"I:\Products\P9. MiMITs\02 Working\MITs\NorMITs-Demand-0.5.2\Export\iter9.16.04\cycle\Lower Model\Matrices"

output_folder = r"I:\Products\P9. MiMITs\02 Working\MITs\NorMITs-Demand-0.5.2\Export\iter9.16.04\cycle\Lower Model\Matrices\lower_only"



#segments_nhb = ["p12"]
segments_hb = ["p1", "p2", "p3", "p4", "p5", "p6", "p7", "p8"]
segments_nhb = ["p12", "p13", "p14", "p15", "p16", "p18"]
#time_periods = ["tp1"]
time_periods = ["tp1", "tp2", "tp3", "tp4"]


for segment in segments_hb:
    demand_filename = "hb_synthetic_pa_yr2023_" + segment + "_m2.csv.bz2"

    # read the demand file
    demand_file = os.path.join(root_folder, demand_filename)
    data = pd.read_csv(demand_file, header=None, compression='bz2', dtype="str",)

    print(f"Processing {segment} hb ...")

    data = data.iloc[0:5840, 0:5840]

    data = data.rename_axis(columns=None).reset_index(drop=True)
    data.loc[0,0] = None

    out_file = os.path.join(output_folder, demand_filename)
    data.to_csv(out_file, index=False, header=False, float_format="%f", compression='bz2')

for segment in segments_nhb:
    for period in time_periods:
        demand_filename = "nhb_synthetic_pa_yr2023_" + segment + "_m2_" + period + ".csv.bz2"

        # read the demand file
        demand_file = os.path.join(root_folder, demand_filename)
        data = pd.read_csv(demand_file, header=None, compression='bz2', dtype="str",)

        print(f"Processing {segment} {period} nhb ...")

        data = data.iloc[0:5840, 0:5840]

        data = data.rename_axis(columns=None).reset_index(drop=True)
        data.loc[0,0] = None

        out_file = os.path.join(output_folder, demand_filename)
        data.to_csv(out_file, index=False, header=False, float_format="%f", compression='bz2')





