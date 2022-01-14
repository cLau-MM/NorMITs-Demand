# -*- coding: utf-8 -*-
"""
    Module for running the NTEM forecast.
"""

##### IMPORTS #####
# Standard imports
import dataclasses
from pathlib import Path
from typing import Dict, Any

# Third party imports

# Local imports
from normits_demand.models import ntem_forecast
from normits_demand import efs_constants as efs_consts
from normits_demand import logging as nd_log

##### CONSTANTS #####
LOG_FILE = "NTEM_forecast.log"
LOG = nd_log.get_logger(
    nd_log.get_package_logger_name() + ".run_models.run_ntem_forecast"
)


##### CLASSES #####
@dataclasses.dataclass(repr=False)
class NTEMForecastParameters:
    """Class for storing the parameters for running NTEM forecasting.

    Attributes
    ----------
    import_path : Path
        Path to the NorMITs demand imports.
    model_name : str
        Name of the model.
    iteration : str
        Iteration number.
    export_path_fmt: str
        Format for the export path, used for building
        `export_path` property.
    export_path_params : Dict[str, Any]
        Dictionary containing any additional parameters
        for building the `export_path`.
    export_path : Path
        Read-only path to export folder, this is built from
        the `export_path_fmt` with variables filled in from
        the class attributes, and additional optional values
        from `export_path_params`.
    """
    import_path: Path = Path("I:/NorMITs Demand/import")
    model_name: str = "noham"
    iteration: str = "1"
    export_path_fmt: str = "I:/NorMITs Demand/{model_name}/NTEM/iter{iteration}"
    export_path_params: Dict[str, Any] = None
    _export_path: Path = dataclasses.field(default=None, init=False, repr=False)

    @property
    def export_path(self) -> Path:
        """
        Read-only path to export folder, this is built from
        the `export_path_fmt` with variables filled in from
        the class attributes, and additional optional values
        from `export_path_params`.
        """
        if self._export_path is None:
            fmt_params = dataclasses.asdict(self)
            if self.export_path_params is not None:
                fmt_params.update(self.export_path_params)
            self._export_path = Path(self.export_path_fmt.format(**fmt_params))
        return self._export_path

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}(import_path={self.import_path!r}, "
            f"model_name={self.model_name!r}, iteration={self.iteration!r}, "
            f"export_path={self.export_path!r})"
        )


##### FUNCTIONS #####
def get_tempro_data() -> ntem_forecast.TEMProTripEnds:
    """Read TEMPro data and convert it to DVectors.

    Returns
    -------
    ntem_forecast.TEMProTripEnds
        TEMPro trip end data as DVectors stored in class
        attributes for base and all future years.
    """
    tempro_data = ntem_forecast.TEMProData(
        [efs_consts.BASE_YEAR] + efs_consts.FUTURE_YEARS
    )
    # Read data and convert to DVectors
    trip_ends = tempro_data.produce_dvectors()
    # Aggregate DVector to required segmentation
    segmentation = ntem_forecast.NTEMImportMatrices.SEGMENTATION
    return trip_ends.aggregate(
        {
            "hb_attractions": segmentation["hb"],
            "hb_productions": segmentation["hb"],
            "nhb_attractions": segmentation["nhb"],
            "nhb_productions": segmentation["nhb"],
        }
    )


def model_mode_subset(
    trip_ends: ntem_forecast.TEMProTripEnds,
    model_name: str,
) -> ntem_forecast.TEMProTripEnds:
    """Get subset of `trip_ends` segmentation for specific `model_name`.

    Parameters
    ----------
    trip_ends : ntem_forecast.TEMProTripEnds
        Trip end data, which has segmentation split by
        mode.
    model_name : str
        Name of the model being ran, currently only
        works for 'noham'.

    Returns
    -------
    ntem_forecast.TEMProTripEnds
        Trip end data at new segmentation.

    Raises
    ------
    NotImplementedError
        If any `model_name` other than 'noham' is
        given.
    """
    model_name = model_name.lower().strip()
    if model_name == "noham":
        segmentation = {
            "hb_attractions": "hb_p_m_car",
            "hb_productions": "hb_p_m_car",
            "nhb_attractions": "nhb_p_m_car",
            "nhb_productions": "nhb_p_m_car",
        }
    else:
        raise NotImplementedError(
            f"NTEM forecasting only not implemented for model {model_name!r}"
        )
    return trip_ends.subset(segmentation)


def main(params: NTEMForecastParameters):
    """Main function for running the NTEM forecasting.

    Parameters
    ----------
    params : NTEMForecastParameters
        Parameters for running NTEM forecasting.

    See Also
    --------
    normits_demand.models.ntem_forecast
    """
    if params.export_path.exists():
        LOG.info("export folder already exists: %s", params.export_path)
    else:
        params.export_path.mkdir(parents=True)
        LOG.info("created export folder: %s", params.export_path)

    tempro_data = get_tempro_data()
    tempro_data = model_mode_subset(tempro_data, params.model_name)
    future_tempro = ntem_forecast.grow_tempro_data(tempro_data)
    future_tempro.save(params.export_path / "TEMProForecasts")

    ntem_inputs = ntem_forecast.NTEMImportMatrices(
        params.import_path,
        efs_consts.BASE_YEAR,
        params.model_name,
    )
    ntem_forecast.grow_all_matrices(
        ntem_inputs,
        future_tempro,
        params.model_name,
        params.export_path / "Matrices",
    )


##### MAIN #####
if __name__ == '__main__':
    # Add log file output to main package logger
    nd_log.get_logger(
        nd_log.get_package_logger_name(), LOG_FILE, "Running NTEM forecast"
    )
    main(NTEMForecastParameters())
