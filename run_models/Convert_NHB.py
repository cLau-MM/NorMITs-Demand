"""Converts NHB from avg weekday to avg hour
"""
import os
import sys
import time

import pandas as pd
import numpy as np

root_folder = r"I:\Products\P9. MiMITs\02 Working\MITs\NorMITs-Demand-0.5.2\Export\iter9.16.01\car_and_passenger\Final Outputs\Full OD Matrices"

output_folder = r"I:\Products\P9. MiMITs\02 Working\MITs\NorMITs-Demand-0.5.2\Export\iter9.16.01\car_and_passenger\Final Outputs\Full OD Matrices\converted time format"



#segments_nhb = ["p12"]
segments_nhb = ["p12", "p13", "p14", "p15", "p16", "p18"]
#time_periods = ["tp1"]
time_periods = ["tp1", "tp2", "tp3", "tp4", "tp5", "tp6"]

pa_od = ["od"]


for segment in segments_nhb:
    for period in time_periods:
        demand_filename = "nhb_synthetic_od_yr2023_" + segment + "_m3_" + period + ".csv.bz2"

        # read the demand file
        demand_file = os.path.join(root_folder, demand_filename)
        data = pd.read_csv(demand_file, header=None, compression='bz2')

        print(f"Processing {segment} {period} od nhb ...")

        data_div = data.div(5)

        data_div[0][1:] = data[0][1:].astype(int)
        data_div.loc[0][1:] = data.loc[0][1:].astype(int)

        out_file = os.path.join(output_folder, demand_filename)
        data_div.to_csv(out_file, index=False, header=False, compression='bz2')





