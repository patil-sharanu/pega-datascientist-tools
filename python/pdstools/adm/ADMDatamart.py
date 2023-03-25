import logging
import os
from datetime import timedelta, datetime
from typing import Dict, NoReturn, Optional, Tuple, Union, Literal, Any
from io import BytesIO
import polars as pl

from ..utils import cdh_utils
from ..utils.types import any_frame
from .ADMTrees import ADMTrees
from ..plots.plot_base import Plots
from ..plots.plots_plotly import ADMVisualisations as plotly_plot
from ..utils.errors import NotEagerError


class ADMDatamart(Plots):
    """Main class for importing, preprocessing and structuring Pega ADM Datamart.
    Gets all available data, properly names and merges into one main dataframe

    Parameters
    ----------
    path : str, default = "."
        The path of the data files
    import_strategy: Literal['eager', 'lazy'], default = 'eager'
        Whether to import the file fully to memory, or scan the file
        When data fits into memory, 'eager' is typically more efficient
        However, when data does not fit, the lazy methods typically allow
        you to still use the data.

    Keyword arguments
    -----------------
    model_filename : Optional[str]
        The name, or extended filepath, towards the model file
    predictor_filename : Optional[str]
        The name, or extended filepath, towards the predictors file
    model_df : Union[pl.DataFrame, pl.LazyFrame, pd.DataFrame]
        Optional override to supply a dataframe instead of a file
    predictor_df : Union[pl.DataFrame, pl.LazyFrame, pd.DataFrame]
        Optional override to supply a dataframe instead of a file
    query : Union[pl.Expr, str, Dict[str, list]], default = None
        Please refer to :meth:`._apply_query`
    plotting_engine : str, default = "plotly"
        Please refer to :meth:`.get_engine`
    subset : bool, default = True
        Whether to only keep a subset of columns for efficiency purposes
        Refer to :meth:`_available_columns` for the default list of columns.
    drop_cols  Optional[list]
        Columns to exclude from reading
    include_cols : Optional[list]
        Additionial columns to include when reading
    context_keys : list, default = ["Channel", "Direction", "Issue", "Group"]
        Which columns to use as context keys
    extract_keys : bool, default = False
        Extra keys, particularly pyTreatment, are hidden within the pyName column.
        extract_keys can expand that cell to also show these values.
        To extract these extra keys, set extract_keys to True.
    predictorCategorization : pl.Expr, default = :meth:`cdh_utils.defaultPredictorCategorization`
        A Polars expression to determine the 'category' of a predictor.
        If None, uses PredictorCategory that is already there.
        See: :meth:`pdstools.utils.cdh_utils.defaultPredictorCategorization`
    verbose : bool, default = False
        Whether to print out information during importing
    **reading_opts
        Additional parameters used while reading.
        Refer to :meth:`pdstools.utils.cdh_utils.import_file` for more info.

    Attributes
    ----------
    modelData : pl.LazyFrame
        If available, holds the preprocessed data about the models
    predictorData : pl.LazyFrame
        If available, holds the preprocessed data about the predictor binning
    combinedData : pl.LazyFrame
        If both modelData and predictorData are available,
        holds the merged data about the models and predictors
    import_strategy
        See the `import_strategy` parameter
    query
        See the `query` parameter
    context_keys
        See the `context_keys` parameter
    verbose
        See the `verbose` parameter


    Examples
    --------
    >>> Data =  ADMDatamart(f"/CDHSample")
    >>> Data =  ADMDatamart(f"Data/Adaptive Models & Predictors Export",
                model_filename = "Data-Decision-ADM-ModelSnapshot_AdaptiveModelSnapshotRepo20201110T085543_GMT/data.json",
                predictor_filename = "Data-Decision-ADM-PredictorBinningSnapshot_PredictorBinningSnapshotRepo20201110T084825_GMT/data.json")
    >>> Data =  ADMDatamart(f"Data/files",
                model_filename = "ModelData.csv",
                predictor_filename = "PredictorData.csv")

    """

    def __init__(
        self,
        path: str = ".",
        import_strategy: Literal["eager", "lazy"] = "eager",
        *,
        model_filename: Optional[str] = "modelData",
        predictor_filename: Optional[str] = "predictorData",
        model_df: Optional[any_frame] = None,
        predictor_df: Optional[any_frame] = None,
        query: Optional[Union[pl.Expr, str, Dict[str, list]]] = None,
        subset: bool = True,
        drop_cols: Optional[list] = None,
        include_cols: Optional[list] = None,
        context_keys: list = ["Channel", "Direction", "Issue", "Group"],
        extract_keys: bool = False,
        predictorCategorization: pl.Expr = cdh_utils.defaultPredictorCategorization,
        plotting_engine: Union[str, Any] = "plotly",
        verbose: bool = False,
        **reading_opts,
    ):
        self.import_strategy = import_strategy
        self.context_keys = context_keys
        self.verbose = verbose
        self.query = query
        self.predictorCategorization = predictorCategorization

        self.modelData, self.predictorData = self.import_data(
            path,
            model_filename=model_filename,
            predictor_filename=predictor_filename,
            model_df=model_df,
            predictor_df=predictor_df,
            subset=subset,
            drop_cols=drop_cols,
            include_cols=include_cols,
            extract_keys=extract_keys,
            **reading_opts,
        )

        if self.modelData is not None and self.predictorData is not None:
            self.combinedData = self._get_combined_data(strategy=self.import_strategy)
        elif verbose:
            print(
                "Could not be combined. Do you have both model data and predictor data?"
            )
        self.context_keys = (
            [key for key in self.context_keys if key in self.modelData.columns]
            if self.modelData is not None
            else context_keys
        )

        self.plotting_engine = plotting_engine
        super().__init__()

    @staticmethod
    def get_engine(plotting_engine):
        """Which engine to use for creating the plots.

        By supplying a custom class here, you can re-use the pdstools functions
        but create visualisations to your own specifications, in any library.
        """
        if not isinstance(plotting_engine, str):
            return plotting_engine
        elif plotting_engine == "plotly":
            return plotly_plot
        else:
            msg = (
                f"Plotting engine {plotting_engine} not known. "
                "Please supply your own class with function names corresponding to plot_base.py "
                "or pass 'Plotly' to use the default engine."
            )
            raise ValueError(msg)

    def import_data(
        self,
        path: Optional[str] = ".",
        *,
        model_filename: Optional[str] = "modelData",
        predictor_filename: Optional[str] = "predictorData",
        model_df: Optional[any_frame] = None,
        predictor_df: Optional[any_frame] = None,
        subset: bool = True,
        drop_cols: Optional[list] = None,
        include_cols: Optional[list] = None,
        extract_keys: bool = False,
        verbose: bool = False,
        **reading_opts,
    ) -> Tuple[Optional[pl.LazyFrame], Optional[pl.LazyFrame]]:
        """Method to import & format the relevant data.

        The method first imports the model data, and then the predictor data.
        If model_df or predictor_df is supplied, it will use those instead
        If any filters are included in the the `query` argument of the ADMDatmart,
        those will be applied to the modeldata, and the predictordata will be
        filtered such that it only contains the modelids leftover after filtering.
        After reading, some additional values (such as success rate) are
        automatically computed.
        Lastly, if there are missing columns from both datasets,
        this will be printed to the user if verbose is True.

        Parameters
        ----------
        path : str
            The path of the data files
            Default = current path (',')
        subset : bool, default = True
            Whether to only select the renamed columns,
            set to False to keep all columns
        model_df : pd.DataFrame
            Optional override to supply a dataframe instead of a file
        predictor_df : pd.DataFrame
            Optional override to supply a dataframe instead of a file

        Returns
        -------
        (pl.DataFrame, pl.DataFrame)
            The model data and predictor binning data as dataframes
        """

        if model_df is not None:
            df1, self.renamed_model, self.missing_model = self._import_utils(
                name=model_df,
                subset=subset,
                drop_cols=drop_cols,
                include_cols=include_cols,
                extract_keys=extract_keys,
                **reading_opts,
            )
        else:
            df1, self.renamed_model, self.missing_model = self._import_utils(
                name=model_filename,
                path=path,
                subset=subset,
                drop_cols=drop_cols,
                include_cols=include_cols,
                extract_keys=extract_keys,
                **reading_opts,
            )
        if df1 is not None:
            df1 = df1.with_columns(
                (pl.col("Positives") / pl.col("ResponseCount"))
                .fill_nan(pl.lit(0))
                .alias("SuccessRate"),
                self._last_timestamp("Positives"),
                self._last_timestamp("ResponseCount"),
            )
        if predictor_df is not None:
            df2, self.renamed_preds, self.missing_preds = self._import_utils(
                name=predictor_df,
                drop_cols=drop_cols,
                include_cols=include_cols,
                subset=subset,
                **reading_opts,
            )
        else:
            df2, self.renamed_preds, self.missing_preds = self._import_utils(
                name=predictor_filename,
                path=path,
                drop_cols=drop_cols,
                include_cols=include_cols,
                subset=subset,
                **reading_opts,
            )
        if df2 is not None:
            if "BinResponseCount" not in df2.columns:  # pragma: no cover
                df2 = df2.with_columns(
                    (pl.col("BinPositives") + pl.col("BinNegatigves")).alias(
                        "BinResponseCount"
                    )
                )
            df2 = df2.with_columns(
                BinPropensity=pl.col("BinPositives") / pl.col("BinResponseCount"),
                BinAdjustedPropensity=(
                    (pl.col("BinPositives") + pl.lit(0.5))
                    / (pl.col("BinResponseCount") + pl.lit(1))
                ),
            )
            if self.predictorCategorization is not None:
                df2 = df2.with_columns(PredictorCategory=self.predictorCategorization())

        if df1 is not None and df2 is not None:
            total_missing = (
                set(self.missing_model)
                & set(self.missing_preds) - set(df1.columns) - set(df2.columns)
            ) - {"Treatment"}
            if len(total_missing) > 0 and verbose:  # pragma: no cover
                print(
                    "Missing expected field values.\n",
                    "Please check if they are available in the data,\n",
                    "and supply a custom mapping if the naming is different from default.\n",
                    f"Missing values: {total_missing}",
                )

            if self.query is not None:
                df2 = df2.join(df1.select(pl.col("ModelID").unique()), on="ModelID")

        if self.import_strategy == "eager":
            if df1 is not None:
                df1 = df1.collect().lazy()
            if df2 is not None:
                df2 = df2.collect().lazy()
        return df1, df2

    def _import_utils(
        self,
        name: Union[str, any_frame],
        path: Optional[str] = None,
        *,
        subset: bool = True,
        extract_keys: bool = False,
        drop_cols: Optional[list] = None,
        include_cols: Optional[list] = None,
        **reading_opts,
    ) -> Tuple[pl.LazyFrame, dict, dict]:
        """Handler function to interface to the cdh_utils methods

        Parameters
        ----------
        name : Union[str, pl.DataFrame]
            One of {modelData, predictorData}
            or a dataframe
        path: str, default = None
            The path of the data file


        Keyword arguments
        -----------------
        subset : bool, default = True
            Whether to only select the renamed columns,
            set to False to keep all columns
        drop_cols : list
            Supply columns to drop from the dataframe
        include_cols : list
            Supply columns to include with the dataframe
        extract_keys : bool
            Treatments are typically hidden within the pyName column,
            extract_keys can expand that cell to also show these values.

        Additional keyword arguments
        -----------------
        See readDSExport in cdh_utils

        Returns
        -------
        (pl.LazyFrame, dict, dict)
            The requested dataframe,
            The renamed columns
            The columns missing in both dataframes
        """

        if isinstance(name, str) or isinstance(name, BytesIO):
            df = cdh_utils.readDSExport(
                filename=name, path=path, verbose=self.verbose, **reading_opts
            )
        elif isinstance(name, pl.DataFrame) or isinstance(name, pl.LazyFrame):
            df = name.lazy()
        else:
            return None, None, None

        self.model_snapshots = True

        df = cdh_utils._polarsCapitalize(df)
        cols, missing = self._available_columns(
            df, include_cols=include_cols, drop_cols=drop_cols
        )
        if subset:
            df = df.select(cols)
        if extract_keys:
            df = self.extract_keys(df)
            df = cdh_utils._polarsCapitalize(df)

        df = self._set_types(
            df,
            timestamp_fmt=reading_opts.get("timestamp_fmt", "%Y%m%dT%H%M%S.%f %Z"),
            strict_conversion=reading_opts.get("strict_conversion", True),
        )

        if self.query is not None:
            try:
                df = self._apply_query(df, self.query)
                if self.verbose:
                    print(f"Query succesful for {name}.")
            except pl.ColumnNotFoundError:
                if self.verbose:
                    print(
                        f"""Query unsuccesful for {name}.
                Maybe the filter you selected only applies to either model data or predictor data
                and thus can't be succesful for the other one. That should be fine
                as the other table is likely queried correctly."""
                    )
        return df, cols, missing

    def _available_columns(
        self,
        df: pl.LazyFrame,
        include_cols: Optional[list] = None,
        drop_cols: Optional[list] = None,
    ) -> Tuple[set, set]:
        """Based on the default names for variables, rename available data to proper formatting

        Parameters
        ----------
        df : pl.LazyFrame
            Input dataframe
        include_cols : list
            Supply columns to include with the dataframe
        drop_cols : list
            Supply columns to not import at all

        Returns
        -------
        (pl.LazyFrame, set, set)
            The original dataframe, but renamed for the found columns &
            The original and updated names for all renamed columns &
            The variables that were not found in the table
        """
        default_names = {
            "ModelID",
            "Issue",
            "Group",
            "Channel",
            "Direction",
            "Name",
            "Treatment",
            "Positives",
            "Configuration",
            "ResponseCount",
            "SnapshotTime",
            "PredictorName",
            "Performance",
            "EntryType",
            "PredictorName",
            "BinSymbol",
            "BinIndex",
            "BinType",
            "BinPositives",
            "BinNegatives",
            "BinResponseCount",
            "Type",
            "Contents",
        }  # NOTE: these default names are already capitalized properly, with py/px/pz removed.

        rename = {i: "Name" for i in df.columns if i.lower() == "modelname"}
        if len(rename) > 0:
            df = df.rename(rename)

        include_cols = (
            set(cdh_utils._capitalize(include_cols)) if include_cols is not None else {}
        )
        drop_cols = (
            set(cdh_utils._capitalize(drop_cols)) if drop_cols is not None else {}
        )

        include_cols = default_names.union(include_cols).difference(drop_cols)
        missing = {col for col in include_cols if col not in df.columns}
        to_import = include_cols.intersection(set(df.columns))

        return to_import, missing

    @staticmethod
    def _set_types(
        df: any_frame,
        *,
        timestamp_fmt: str = "%Y%m%dT%H%M%S.%f %Z",
        strict_conversion: bool = True,
    ) -> any_frame:
        """A method to change columns to their proper type

        Parameters
        ----------
        df : Union[pl.DataFrame, pl.LazyFrame]
            The input dataframe

        Keyword arguments
        -----------------
        timestamp_fmt: str
            The format of Date type columns
        strict_conversion: bool
            Raises an error if timestamp conversion to given/default date format(timestamp_fmt) fails
            See 'https://strftime.org/' for timestamp formats
        Returns
        -------
        Union[pl.DataFrame, pl.LazyFrame]
            The input dataframe, but the proper typing applied
        """
        retype = {
            pl.Categorical: ["Issue", "Group", "Channel", "Direction", "Configuration"],
            # pl.Int64: ["Positives", "Negatives", "ResponseCount"],
            pl.Float64: ["Performance"],
        }
        to_retype = []
        for type, cols in retype.items():
            for col in cols:
                if col in set(df.columns):
                    to_retype.append(pl.col(col).cast(type))
        df = df.with_columns(to_retype)
        if "SnapshotTime" not in df.columns:
            df = df.with_columns(SnapshotTime=None)
        elif df.schema["SnapshotTime"] == pl.Utf8:
            df = df.with_columns(
                pl.col("SnapshotTime").str.strptime(
                    pl.Datetime,
                    timestamp_fmt,
                    strict=strict_conversion,
                )
            )
        return df

    def last(
        self, table="modelData", strategy: Literal["eager", "lazy"] = "eager"
    ) -> any_frame:
        """Convenience function to get the last values for a table

        Parameters
        ----------
        table : str, default = modelData
            Which table to get the last values for
            One of {modelData, predictorData, combinedData}
        strategy: Literal['eager', 'lazy'], default = 'eager'
            Whether to import the file fully to memory, or scan the file
            When data fits into memory, 'eager' is typically more efficient
            However, when data does not fit, the lazy methods typically allow
            you to still use the data.

        Returns
        -------
        Union[pl.DataFrame, pl.LazyFrame]
            The last snapshot for each model
        """

        if isinstance(table, pl.LazyFrame):
            df = self._last(table)

        elif isinstance(table, str):
            assert table in {"modelData", "predictorData", "combinedData"}
            df = self._last(getattr(self, table))
        else:  # pragma: no cover
            raise ValueError("This should not happen, please file a GitHub issue :).")
        return df if not strategy == "eager" else df.collect()

    @staticmethod
    def _last(df: any_frame) -> any_frame:
        """Method to retrieve only the last snapshot."""
        return df.filter(
            pl.col("SnapshotTime").fill_null(1)
            == pl.col("SnapshotTime").fill_null(1).max()
        )

    @staticmethod
    def _last_timestamp(col: Literal["ResponseCount", "Positives"]) -> pl.Expr:
        """Add a column to indicate the last timestamp a column has changed.

        Parameters
        ----------
        col : Literal['ResponseCount', 'Positives']
            The column to calculate the diff for
        """

        return (
            pl.col("SnapshotTime")
            .where(pl.col(col).diff() != 0)
            .max()
            .over("ModelID")
            .alias(f"Last_{col}")
        )

    def _get_combined_data(
        self, last=True, strategy: Literal["eager", "lazy"] = "eager"
    ) -> any_frame:
        """Combines the model data and predictor data into one dataframe.

        Parameters
        ----------
        last:bool, default=True
            Whether to only use the last snapshot for each table
        strategy: Literal['eager', 'lazy'], default = 'eager'
            Whether to import the file fully to memory, or scan the file
            When data fits into memory, 'eager' is typically more efficient
            However, when data does not fit, the lazy methods typically allow
            you to still use the data.

        Returns
        -------
        Union[pl.DataFrame, pl.LazyFrame]
            The combined dataframe
        """
        models = self.last(self.modelData, "lazy") if last else self.modelData
        preds = self.last(self.predictorData, "lazy") if last else self.predictorData
        combined = models.join(preds, on="ModelID", how="inner", suffix="Bin")
        if strategy == "eager":
            return combined.collect().lazy()
        else:
            return combined

    def save_data(self, path: str = ".") -> Tuple[os.PathLike, os.PathLike]:
        """Cache modelData and predictorData to files.

        Parameters
        ----------
        path : str
            Where to place the files

        Returns
        -------
        (os.PathLike, os.PathLike):
            The paths to the model and predictor data files
        """
        from datetime import datetime

        time = datetime.now().strftime("%Y%m%dT%H%M%S.%f")[:-3]
        if self.modelData is not None:
            modeldata_cache = cdh_utils.cache_to_file(
                self.modelData, path, name=f"cached_modelData_{time}"
            )
        if self.predictorData is not None:
            predictordata_cache = cdh_utils.cache_to_file(
                self.predictorData, path, name=f"cached_predictorData_{time}"
            )
        return modeldata_cache, predictordata_cache

    def _apply_query(
        self,
        df: any_frame,
        query: Optional[Union[pl.Expr, str, Dict[str, list]]] = None,
    ) -> pl.LazyFrame:
        """Given an input pandas dataframe, it filters the dataframe based on input query

        Parameters
        ----------
        df : Union[pl.DataFrame, pl.LazyFrame]
            The input dataframe
        query: Optional[Union[pl.Expr, str, Dict[str, list]]]
            If a Polars Expression, passes the expression into Polars' filter function
            If a string, uses the default Pandas query function (works only in eager mode)
            Else, a dict of lists where the key is column name in the dataframe
            and the corresponding value is a list of values to keep in the dataframe
        Returns
        -------
        pl.DataFrame
            Filtered Polars DataFrame
        """
        if isinstance(df, pl.DataFrame):
            df = df.lazy()
        if query is not None:
            if isinstance(query, pl.Expr):
                col_diff = set(query.meta.root_names()) - set(df.columns)
                if len(col_diff) == 0:
                    return df.filter(query)

                else:
                    raise pl.ColumnNotFoundError(col_diff)

            if isinstance(query, str):
                print(
                    "Pandas query detected. This will be slow, and only works in eager mode ",
                    "because we turn the dataframe to pandas, query, and then turn it back ",
                    "to Polars. For faster performance, please pass a Polars Expression ",
                    "which can be used in the `filter()` method.",
                )
                if not self.import_strategy == "eager":
                    raise NotEagerError("Applying pandas queries")
                else:
                    return pl.LazyFrame(
                        df.collect()
                        .to_pandas(use_pyarrow_extension_array=True)
                        .query(query)
                    )

            if not isinstance(query, dict):
                raise TypeError("query must be a dict where values are lists")
            for val in query.values():
                if not type(val) == list:
                    raise ValueError("query values must be list")

            for col, val in query.items():
                df = df.filter(pl.col(col).is_in(val))
        return df

    def extract_keys(
        self,
        df,
        col="Name",
    ):
        if self.import_strategy != "eager":
            raise NotEagerError("Extracting keys")
        if self.verbose:
            print("Extracting keys...")

        def safeName():
            return (
                pl.when(~pl.col("Name").cast(pl.Utf8).str.starts_with("{"))
                .then(
                    pl.concat_str([pl.lit('{"pyName":"'), pl.col("Name"), pl.lit('"}')])
                )
                .otherwise(pl.col("Name"))
                .alias("tempName")
            )

        return (
            (
                df.with_columns(
                    safeName().str.json_extract(),
                )
                .drop(col)
                .unnest("tempName")
            )
            .collect()
            .lazy()
        )

    def discover_modelTypes(
        self, df: pl.LazyFrame, by="Configuration"
    ):  # pragma: no cover
        if self.import_strategy != "eager":
            raise NotEagerError("Discovering AGB models")
        if "Modeldata" not in df.columns:
            raise ValueError(
                (
                    "Modeldata column not in the data. "
                    "Please make sure to include it by using the 'include_cols' "
                    "argument with your ADMDatamart call, or setting 'subset' to False."
                )
            )

        def _getType(val):
            import zlib
            import base64

            return next(
                line.split('"')[-2].split(".")[-1]
                for line in zlib.decompress(base64.b64decode(val)).decode().split("\n")
                if line.startswith('  "_serialClass"')
            )

        if isinstance(df, pl.DataFrame):
            df = df.lazy()
        types = (
            df.filter(pl.col("Modeldata").is_not_null())
            .groupby(by)
            .agg(pl.col("Modeldata").last())
            .collect()
            .with_columns(pl.col("Modeldata").apply(lambda v: _getType(v)))
            .to_dicts()
        )
        return {key: value for key, value in [i.values() for i in types]}

    def get_AGB_models(
        self,
        last: bool = False,
        by: str = "Configuration",
        n_threads: int = 1,
        query: Optional[Union[pl.Expr, str, Dict[str, list]]] = None,
        verbose: bool = True,
        **kwargs,
    ) -> Dict:  # pragma: no cover
        """Method to automatically extract AGB models.

        Recommended to subset using the querying functionality
        to cut down on execution time, because it checks for each
        model ID. If you only have AGB models remaining after the query,
        it will only return proper AGB models.

        Parameters
        ----------
        last: bool, default = False
            Whether to only look at the last snapshot for each model
        by: str, default = 'Configuration'
            Which column to determine unique models with
        n_threads: int, default = 6
            The number of threads to use for extracting the models.
            Since we use multithreading, setting this to a reasonable value
            helps speed up the import.
        query: Optional[Union[pl.Expr, str, Dict[str, list]]]
            Please refer to :meth:`._apply_query`
        verbose: bool, default = False
            Whether to print out information while importing

        """
        df = self.modelData
        if query is not None:
            df = self._apply_query(df, query)

        modelTypes = self.discover_modelTypes(df)
        AGB_models = [
            model for model, type in modelTypes.items() if type.endswith("GbModel")
        ]
        logging.info(f"Found AGB models: {AGB_models}")
        df = df.filter(pl.col("Configuration").is_in(AGB_models))
        if df.select(pl.col("ModelID").n_unique()).collect().item() == 0:
            raise ValueError("No models found.")

        if last:
            return ADMTrees(
                self.last(df), n_threads=n_threads, verbose=verbose, **kwargs
            )
        else:
            return ADMTrees(
                df.select("Configuration", "SnapshotTime", "Modeldata").collect(),
                n_threads=n_threads,
                verbose=verbose,
                **kwargs,
            )

    @staticmethod
    def _create_sign_df(
        df: pl.LazyFrame,
        by: str = "Name",
        *,
        what: str = "ResponseCount",
        every: str = "1d",
        pivot: bool = True,
        mask: bool = True,
    ) -> pl.LazyFrame:
        """Generates dataframe to show whether responses decreased/increased from day to day

        For a given dataframe where columns are dates and rows are model names(by parameter),
        subtracts each day's value from the previous day's value per model. Then masks the data.
        If increased (desired situtation), it will put 1 in the cell, if no change, it will
        put 0, and if decreased it will put -1. This dataframe then could be used in the heatmap

        Parameters
        ----------
        df: pd.DataFrame
            This is typically pivoted ModelData
        by: str, default = Name
            Column to calculate the daily change for.

        Keyword arguments
        -----------------
        what: str, default = ResponseCount
            Column that contains response counts
        every: str, default = 1d
            Interval of the change window
        pivot: bool, default = True
            Returns a pivotted table with signs as value if set to true
        mask: bool, default = True
            Drops SnapshotTime and returns direction of change(sign).

        Returns
        -------
        pd.LazyFrame
            The dataframe with signs for increase or decrease in day to day
        """

        df = (
            df.with_columns(pl.col("SnapshotTime").cast(pl.Date))
            .sort("SnapshotTime")
            .with_columns(
                pl.col(what)
                .cast(pl.Int64)
                .diff()
                .alias("Daily_increase")
                .over("ModelID")
            )
            .groupby_dynamic("SnapshotTime", every=every, by=by)
            .agg(pl.sum("Daily_increase").alias("Increase"))
        )
        if pivot:
            df = (
                df.collect()
                .pivot(index="SnapshotTime", columns=by, values="Increase")
                .lazy()
            )
        if mask:
            df = df.with_columns((pl.all().exclude([by, "SnapshotTime"]).sign()))
        return df

    def model_summary(
        self,
        by: str = "ModelID",
        query: Optional[Union[pl.Expr, str, Dict[str, list]]] = None,
        **kwargs,
    ) -> pl.LazyFrame:
        """Convenience method to automatically generate a summary over models

        By default, it summarizes ResponseCount, Performance, SuccessRate & Positives by model ID.
        It also adds weighted means for Performance and SuccessRate,
        And adds the count of models without responses and the percentage.

        Parameters
        ----------
        by: str, default = ModelID
            By what column to summarize the models
        query: Optional[Union[pl.Expr, str, Dict[str, list]]]
            Please refer to :meth:`._apply_query`

        Returns
        -------
        pl.LazyFrame:
            Groupby dataframe over all models
        """
        df = self._apply_query(self.modelData, query)
        data = self.last(df, strategy="lazy").lazy()

        aggcols = ["ResponseCount", "Performance", "SuccessRate", "Positives"]
        required_columns = set(aggcols).union({by})

        context_keys = kwargs.get("context_keys", self.context_keys)
        assert required_columns.issubset(set(data.columns) | set(context_keys))

        return (
            data.groupby(context_keys)
            .agg(
                [
                    pl.count(by).suffix("_count"),
                    pl.col([aggcols[0], aggcols[3]]).sum().suffix("_sum"),
                    pl.col(aggcols).max().suffix("_max"),
                    pl.col(aggcols).mean().suffix("_mean"),
                    (pl.col("ResponseCount") == 0)
                    .sum()
                    .alias("Count_without_responses"),
                    (
                        cdh_utils.weighed_performance_polars().alias(
                            "Performance_weighted"
                        )
                    ),
                    cdh_utils.weighed_average_polars(
                        "SuccessRate", "ResponseCount"
                    ).alias("SuccessRate_weighted"),
                ],
            )
            .with_columns(
                (pl.col("Count_without_responses") / pl.col(f"{by}_count")).alias(
                    "Percentage_without_responses"
                )
            )
        )

    def pivot_df(
        self,
        df: pl.LazyFrame,
        by: Union[str, list] = "Name",
        *,
        allow_collect: bool = True,
        top_n: int = 0,
    ) -> pl.DataFrame:
        """Simple function to extract pivoted information

        Parameters
        ----------
        df : pl.LazyFrame
            The input DataFrame.
        by : Union[str, list], default = Name
            The column(s) to pivot the DataFrame by.
            If a list is provided, only the first element is used.
        allow_collect : bool, default = True
            Whether to allow eager computation.
            If set to False and the import strategy is "lazy", an error will be raised.
        top_n : int, optional (default=0)
            The number of rows to include in the pivoted DataFrame.
            If set to 0, all rows are included.

        Returns
        -------
        pl.DataFrame
            The pivoted DataFrame.
        """

        if isinstance(by, list):
            by = by[0]
        if self.import_strategy == "lazy" and not allow_collect:
            raise NotEagerError("Pivot df.")
        df = df.filter(pl.col("PredictorName") != "Classifier")
        if by not in ["ModelID", "Name"]:
            df = (
                df.with_columns(pl.col("ResponseCount"))
                .groupby([by, "PredictorName"])
                .agg(
                    cdh_utils.weighed_average_polars("PerformanceBin", "ResponseCount")
                )
            )

        if top_n > 0:
            df = (
                df.with_columns(pl.col("PerformanceBin").fill_nan(0.5))
                .sort("PerformanceBin", descending=True)
                .head(top_n)
            )
        df = (
            df.collect()
            .pivot(index=by, columns="PredictorName", values="PerformanceBin")
            .fill_null(0.5)
            .fill_nan(0.5)
        )
        mod_order = (
            df.select(
                pl.concat_list(pl.col(pl.Float64))
                .arr.eval(pl.element().mean())
                .arr.get(0)
            )
            .select(pl.all().arg_sort(descending=True))
            .to_series()
        )
        pred_order = [by] + [
            df.columns[i + 1]
            for i in 0
            + df.select(pl.col(pl.Float64).mean())
            .transpose()
            .select(pl.all().arg_sort(descending=True))
            .to_series()
        ]
        return df[mod_order].select(pred_order)

    @staticmethod
    def response_gain_df(df: any_frame, by: str = "Channel") -> any_frame:
        """Simple function to extract the response gain per model"""
        if isinstance(by, list):
            by = by[0]
        return (
            df.groupby([by, "ModelID"])
            .agg(pl.max("ResponseCount"))
            .sort([by, "ResponseCount"], descending=True)
            .with_columns(
                [
                    (pl.cumsum("ResponseCount") / pl.sum("ResponseCount"))
                    .over(by)
                    .alias("TotalResponseFraction"),
                    ((pl.col(by).cumcount() + 1) / pl.count("ResponseCount"))
                    .over(by)
                    .alias("TotalModelsFraction"),
                ]
            )
        )

    def models_by_positives_df(
        self, df: pl.LazyFrame, by: str = "Channel", allow_collect=True
    ) -> pl.LazyFrame:
        """
        Compute statistics on the dataframe by grouping it by a given column `by`
        and computing the count of unique ModelIDs and cumulative percentage of unique
        models for with regard to the number of positive answers.

        Parameters
        ----------
        df : pl.LazyFrame
            The input DataFrame
        by : str, default = Channel
            The column name to group the DataFrame by, by default "Channel"
        allow_collect : bool, default = True
            Whether to allow eager computation. If set to False and the import strategy is "lazy", an error will be raised.

        Returns
        -------
        pl.LazyFrame
           DataFrame with PositivesBin column and model count statistics
        """
        if self.import_strategy == "lazy" and not allow_collect:
            raise NotEagerError("Models by positive df.")

        def orderedCut(
            s, label="PositivesBin", bins=list(range(0, 210, 10))
        ) -> pl.Series:
            _arg_sort = pl.Series(name="_sort", values=s.arg_sort())
            result = pl.cut(
                s, bins=[bins + [float("inf")]][0], category_label="PositivesBin"
            )
            return (
                result.select(
                    [
                        pl.col(label),
                        _arg_sort,
                    ]
                )
                .sort("_sort")
                .drop("_sort")
                .to_series()
            )

        modelsByPositives = df.select([by, "Positives", "ModelID"]).collect()
        return (
            modelsByPositives.hstack(
                [
                    orderedCut(
                        modelsByPositives["Positives"],
                    )
                ]
            )
            .lazy()
            .groupby([by, "PositivesBin"])
            .agg([pl.min("Positives"), pl.n_unique("ModelID").alias("ModelCount")])
            .with_columns(
                (pl.col("ModelCount") / (pl.sum("ModelCount").over(by))).alias(
                    "cumModels"
                )
            )
            .sort("PositivesBin")
        )

    def get_model_stats(self, last: bool = True) -> dict:
        """Returns a dictionary containing various statistics for the model data.

        Parameters
        ----------
        last : bool
            Whether to compute statistics only on the last snapshot. Defaults to True.

        Returns
        -------
        Dict
            A dictionary containing the following keys:
            'models_n_snapshots': The number of distinct snapshot times in the data.
            'models_total': The total number of models in the data.
            'models_empty': The models with no responses.
            'models_nopositives': The models with responses but no positive responses.
            'models_isimmature': The models with less than 200 positive responses.
            'models_noperformance': The models with at least 200 positive responses but a performance of 50.
            'models_n_nonperforming': The total number of models that are not performing well.
            'models_missing_{key}': The number of models with missing values for each context key.
            'models_bottom_left': The models with a performance of 50 and a success rate of 0.
        """
        if self.modelData is None:
            raise ValueError("No model data to analyze.")

        data = self.last(self.modelData) if last else self.modelData

        ret = dict()
        ret["models_n_snapshots"] = data.select(pl.n_unique("SnapshotTime")).item()
        ret["models_total"] = len(data)
        ret["models_empty"] = data.filter(pl.col("ResponseCount") == 0)
        ret["models_nopositives"] = data.filter(
            (pl.col("ResponseCount") > 0) & (pl.col("Positives") == 0)
        )
        ret["models_isimmature"] = data.filter(pl.col("Positives").is_between(0, 200))
        ret["models_noperformance"] = data.filter(
            (pl.col("Positives") >= 200) & (pl.col("Performance") == 0.5)
        )
        ret["models_n_nonperforming"] = (
            len(ret["models_empty"])
            + len(ret["models_nopositives"])
            + len(ret["models_isimmature"])
            + len(ret["models_noperformance"])
        )
        for key in self.context_keys:
            ret[f"models_missing_{key}"] = data.filter(pl.col(key).is_null())
        ret["models_bottom_left"] = data.filter(
            (pl.col("Performance") == 0.5) & (pl.col("SuccessRate") == 0)
        )
        self.model_stats = ret
        return ret

    def describe_models(self, **kwargs) -> NoReturn:
        """Convenience method to quickly summarize the models"""
        if not hasattr(self, "model_stats"):
            self.get_model_stats(last=True)
        assert {"models_empty"}.issubset(self.model_stats.keys())
        show_all_missing = kwargs.get("show_all_missing", True)
        nmodels = self.model_stats.get("models_total")

        ret = ""
        ret += f"""From all {nmodels} models:
{len(self.model_stats['models_empty'])} ({round(len(self.model_stats['models_empty'])/nmodels*100,2)}%) models have never recieved a response.
{len(self.model_stats['models_nopositives'])} ({round(len(self.model_stats['models_nopositives'])/nmodels*100,2)}%) models have been used but never recieved a 'positive' response.
{len(self.model_stats['models_isimmature'])} ({round(len(self.model_stats['models_isimmature'])/nmodels*100,2)}%) models are still in an 'immature' phase of learning (Positives between 1 and 200).
{len(self.model_stats['models_noperformance'])} ({round(len(self.model_stats['models_noperformance'])/nmodels*100,2)}%) models have recieved over 200 responses but still show minimum performance.
Meaning in total, {self.model_stats['models_n_nonperforming']} ({round(self.model_stats['models_n_nonperforming']/nmodels*100)}%) models do not perform as well as they could be.\n\n"""

        for key in self.model_stats.keys():
            if not isinstance(self.model_stats[key], int):
                if len(self.model_stats[key]) > 0 or show_all_missing:
                    ret += f"{len(self.model_stats[key])} ({round(len(self.model_stats[key])/nmodels*100,2)}%) {' '.join(key.split('_'))} attribute.\n"

        return ret

    def generateHealthCheck(
        self,
        name: Optional[str] = None,
        output_location=".",
        working_dir=".",
        delete_temp_files=True,
        output_type="html",
        allow_collect=True,
    ):
        """Manually generates a Health Check

        The recommended way to run the Health Check is through the Streamlit app,
        which you get to by running `pdstools run` in your terminal/command line.
        However, it's also possible to directly run the Health Check from the
        ADMDatamart class, providing a little bit more flexibility.

        Internally, this method uses :meth:`.save_data` to save the model and
        predictor files to disk, which Quarto then picks up to generate.

        By running this method directly, you have control over the exact
        filters you want to use. Simply use the top-level :attr:`.query`
        argument to filter the ADMDatamart class to the desired level.

        Parameters
        ----------
        name : Optional[str]
            The name of the Health Check file
        output_location : str, default = '.'
            The output location of the generated file
        working_dir : str, default = '.'
            The directory in which to create the health check
        delete_temp_files : bool, default = True
            Whether to delete the temporary files used  while generating the file
            If false, these files stay in working_dir
        output_type: : str, default = 'html'
            Which type of export to create. Currently, html is best supported.
        allow_collect : bool, default = True
            An override for the `lazy` memory_strategy. If set to True, still allows
            for collecting of data. Naturally, we need to collect the data in order
            to cache it to disk for the health check, so if set to False
            with a `lazy` memory_strategy, you won't be able to generate.

        Returns
        -------
        str:
            The full path to the generated Health Check file.
        """

        import shutil
        import subprocess
        from pdstools import __healthcheck_file__
        import yaml

        if self.modelData is None or self.predictorData is None:
            raise AssertionError("Needs both model and predictor data.")
        if self.import_strategy == "lazy" and not allow_collect:
            raise NotEagerError("Generating healthcheck")
        if not os.path.exists(working_dir):
            os.mkdir(working_dir)

        shutil.copy(__healthcheck_file__, working_dir)
        if name is not None:
            output_filename = f"HealthCheck_{name.replace(' ', '_')}.{output_type}"
        else:
            output_filename = f"ADM_HealthCheck.{output_type}"

        files = self.save_data(working_dir)

        params = {"kwargs": {"subset": False, "predictorCategorization": None}}

        with open(f"{working_dir}/params.yaml", "w") as f:
            yaml.dump(params, f)

        bashCommand = f"quarto render HealthCheck.qmd --to {output_type} --output {output_filename} --execute-params params.yaml"
        if not self.verbose:
            stdout, stderr = subprocess.DEVNULL, subprocess.STDOUT
        else:
            print("Set verbose=False to hide output.")
            print("Running:", bashCommand)
            stdout, stderr = subprocess.PIPE, None
        process = subprocess.Popen(
            bashCommand.split(), stdout=stdout, stderr=stderr, cwd=working_dir
        )
        process.communicate()

        if output_location != working_dir:
            shutil.move(f"{working_dir}/{output_filename}", output_location)

        if delete_temp_files:
            for f in ["params.yaml", "HealthCheck.qmd"]:
                os.remove(f"{working_dir}/{f}")
            for f in files:
                os.remove(f)
        return f"{output_location}/{output_filename}"