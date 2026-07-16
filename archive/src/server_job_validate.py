"""A sub-module for running validation commands on the server."""

import os

import coastline_mask_OLD
import validate_dem
import validate_dem_collection


def run_validate_command(ivert_job_obj):
    ivj = ivert_job_obj
    job_row = ivj.jobs_db.job_exists(ivj.username, ivj.job_id, return_row=True)
    assert job_row

    jco = ivj.job_config_object
    assert hasattr(jco, "cmd_args") and type(jco.cmd_args) is dict
    assert hasattr(jco, "files") and isinstance(jco.files, list) and len(jco.files) > 0
    cargs = jco.cmd_args
    assert "input_vdatum" in cargs
    # For now, default to egm2008 for the output vdatum. Later, will switch to modifying the photons to meet the input vdatum.
    # assert "output_vdatum" in cargs
    # TODO: Remove this line after modifhying the photons to meet the input vdatum.
    cargs["output_vdatum"] = "egm2008"

    # Maintain backward compatibility with previous IVERT versions.
    if "mask_buildings" in cargs:
        cargs["mask_bing_buildings"] = cargs["mask_buildings"]
        cargs["mask_osm_buildings"] = cargs["mask_buildings"]
        del cargs["mask_buildings"]

    if "mask_urban" in cargs:
        cargs["mask_wsf_urban"] = cargs["mask_urban"]
        del cargs["mask_urban"]

    assert "region_name" in cargs
    assert "measure_coverage" in cargs
    assert "include_photons" in cargs
    assert "band_num" in cargs
    assert "coastlines_only" in cargs
    assert "mask_osm_buildings" in cargs
    assert "mask_bing_buildings" in cargs
    assert "mask_wsf_urban" in cargs
    assert "outlier_sd_threshold" in cargs

    outputs_dir = str(os.path.join(ivj.job_dir, "outputs"))
    if not os.path.exists(outputs_dir):
        os.mkdir(outputs_dir)

    dem_files = [str(os.path.join(ivj.job_dir, f)) for f in jco.files]

    # Handle logic of coastline_only. Don't call validate, just generate the coastline mask.
    if cargs["coastlines_only"]:
        for dem_fn in dem_files:
            ivj.update_file_status(
                os.path.basename(dem_fn), "processing", upload_to_s3=False
            )

            cfile = coastline_mask_OLD.create_coastline_mask(
                dem_fn,
                mask_out_lakes=True,
                mask_osm_buildings=cargs["mask_osm_buildings"],
                mask_bing_buildings=cargs["mask_bing_buildings"],
                mask_wsf_urban=cargs["mask_wsf_urban"],
                mask_out_nhd=True,
                run_in_tempdir=True,
                horizontal_datum_only=True,
                verbose=ivj.verbose,
            )

            if cfile is None or not os.path.exists(cfile):
                ivj.update_file_status(
                    os.path.basename(dem_fn), "error", upload_to_s3=False
                )
            else:
                ivj.upload_file_to_s3(cfile, upload_to_s3=False)

                # Mark the input file as "processed."
                ivj.update_file_status(
                    os.path.basename(dem_fn), "processed", upload_to_s3=False
                )

    elif len(dem_files) == 1:
        # If there's only one file, run the validate_dem module.
        validate_dem.validate_dem(
            dem_files[0],
            output_dir=outputs_dir,
            shared_ret_values=None,
            icesat2_photon_database_obj=None,
            ivert_job_name=f"{ivj.username}_{ivj.job_id}",
            dem_vertical_datum=cargs["input_vdatum"],
            output_vertical_datum=cargs["output_vdatum"],
            mask_out_lakes=True,
            mask_osm_buildings=cargs["mask_osm_buildings"],
            mask_bing_buildings=cargs["mask_bing_buildings"],
            mask_wsf_urban=cargs["mask_wsf_urban"],
            write_result_tifs=True,
            write_summary_stats=True,
            export_coastline_mask=True,
            plot_results=True,
            outliers_sd_threshold=cargs["outlier_sd_threshold"],
            location_name=cargs["region_name"],
            mark_empty_results=True,
            omit_bad_granules=True,
            measure_coverage=cargs["measure_coverage"],
            include_photon_level_validation=cargs["include_photons"],
            band_num=cargs["band_num"],
            verbose=ivj.verbose,
        )

    else:
        # Run the validate_dem_collection module.
        validate_dem_collection.validate_list_of_dems(
            dem_files,
            output_dir=outputs_dir,
            fname_filter=None,
            fname_omit=None,
            ivert_job_name=f"{ivj.username}_{ivj.job_id}",
            band_num=cargs["band_num"],
            input_vdatum=cargs["input_vdatum"],
            output_vdatum=cargs["output_vdatum"],
            overwrite=False,
            place_name=cargs["region_name"],
            mask_osm_buildings=cargs["mask_osm_buildings"],
            mask_bing_buildings=cargs["mask_bing_buildings"],
            mask_wsf_urban=cargs["mask_wsf_urban"],
            create_individual_results=True,
            delete_datafiles=False,
            include_photon_validation=cargs["include_photons"],
            write_result_tifs=True,
            omit_bad_granules=True,
            write_summary_csv=True,
            measure_coverage=cargs["measure_coverage"],
            outliers_sd_threshold=cargs["outlier_sd_threshold"],
            verbose=ivj.verbose,
        )

    print(
        "===============================================\n"
        f"Job {ivj.username}_{ivj.job_id} is complete.\n"
        "==============================================="
    )

    return
