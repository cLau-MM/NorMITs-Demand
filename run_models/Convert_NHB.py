"""Converts NHB from avg weekday to avg hour
"""
import os
import sys
import time

import pandas as pd
import numpy as np

root_folder = r"I:\Products\P9. MiMITs\02 Working\MITs\NorMITs-Demand-0.5.2\Export\iter9.16.04\cycle\Final Outputs\Full OD Matrices"

output_folder = r"I:\Products\P9. MiMITs\02 Working\MITs\NorMITs-Demand-0.5.2\Export\iter9.16.04\cycle\Final Outputs\Full OD Matrices\converted time format"



#segments_nhb = ["p12"]
segments_nhb = ["p12", "p13", "p14", "p15", "p16", "p18"]
#time_periods = ["tp1"]
time_periods = ["tp1", "tp2", "tp3", "tp4"]

pa_od = ["od"]

for segment in segments_nhb:
    for period in time_periods:
        demand_filename = "nhb_synthetic_od_yr2023_" + segment + "_m2_" + period + ".csv.bz2"

        # read the demand file
        demand_file = os.path.join(root_folder, demand_filename)
        data = pd.read_csv(demand_file, header=None, compression='bz2',dtype=str)

        print(f"Processing {segment} {period} od nhb ...")

        data_div = data.iloc[1:,1:].astype(float).div(5)
        data_div.insert(0, None, data.iloc[1:,0])
        data_div.loc[-1] = data.iloc[0,:]
        data_div.index = data_div.index+1
        data_div.sort_index(inplace=True)

        out_file = os.path.join(output_folder, demand_filename)
        data_div.to_csv(out_file, index=False, header=False, compression='bz2')





