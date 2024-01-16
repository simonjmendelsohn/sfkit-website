import io
import uuid
from datetime import datetime

from google.cloud import firestore
from quart import Blueprint, Response, current_app, jsonify, request, send_file
from werkzeug.exceptions import BadRequest, Conflict, Forbidden

from src.auth import authenticate, get_user_id
from src.utils import constants, custom_logging
from src.utils.google_cloud.google_cloud_compute import (GoogleCloudCompute,
                                                         format_instance_name)
from src.utils.studies_functions import make_auth_key, study_title_already_exists

logger = custom_logging.setup_logging(__name__)
bp = Blueprint("study", __name__, url_prefix="/api")


@bp.route("/study", methods=["GET"])
@authenticate
async def study() -> Response:
    user_id = await get_user_id()
    study_id = request.args.get("study_id")
    db: firestore.AsyncClient = current_app.config["DATABASE"]

    try:
        study = (await db.collection("studies").document(study_id).get()).to_dict()
    except Exception as e:
        logger.error(f"Failed to fetch study: {e}")
        raise Forbidden() 

    if user_id not in study["participants"]:
        raise Forbidden()

    try:
        display_names = (
            await db.collection("users").document("display_names").get()
        ).to_dict() or {}
    except Exception as e:
        logger.error(f"Failed to fetch display names: {e}")
        raise BadRequest()
        

    study["owner_name"] = display_names.get(study["owner"], study["owner"])
    study["display_names"] = {
        participant: display_names.get(participant, participant)
        for participant in study["participants"]
        + list(study["requested_participants"].keys())
        + study["invited_participants"]
    }

    return jsonify({"study": study})

# TODO: use asyncio to delete in parallel. This requires making the google_cloud_compute functions async. Using multiple processing failed because inside daemon. Threads failed because of GIL.
@bp.route("/restart_study", methods=["GET"])
@authenticate
async def restart_study() -> Response:
    study_id = request.args.get("study_id")
    db: firestore.AsyncClient = current_app.config["DATABASE"]
    doc_ref = db.collection("studies").document(study_id)
    doc_ref_dict: dict = (await doc_ref.get()).to_dict()

    for role, v in enumerate(doc_ref_dict["participants"]):
        participant = doc_ref_dict["personal_parameters"][v]
        if (gcp_project := participant.get("GCP_PROJECT").get("value")) != "":
            google_cloud_compute = GoogleCloudCompute(study_id, gcp_project)
            for instance in google_cloud_compute.list_instances():
                if instance == format_instance_name(
                    google_cloud_compute.study_id, str(role)
                ):
                    google_cloud_compute.delete_instance(instance)

            google_cloud_compute.delete_firewall(None)
    logger.info("Successfully Deleted gcp instances and firewalls")

    for participant in doc_ref_dict["participants"]:
        doc_ref_dict["status"][participant] = (
            "ready to begin protocol" if participant == "Broad" else ""
        )
        doc_ref_dict["personal_parameters"][participant]["PUBLIC_KEY"]["value"] = ""
        doc_ref_dict["personal_parameters"][participant]["IP_ADDRESS"]["value"] = ""
    doc_ref_dict["tasks"] = {}
    await doc_ref.set(doc_ref_dict)

    return jsonify({"message": "Successfully restarted study"})


@bp.route("/create_study", methods=["POST"])
@authenticate
async def create_study() -> Response:
    user_id = await get_user_id()

    data: dict = await request.json
    study_type = data.get("study_type")
    setup_configuration = data.get("setup_configuration")
    study_title = data.get("title")
    demo = data.get("demo_study")
    private_study = data.get("private_study")
    description = data.get("description")
    study_information = data.get("study_information")

    logger.info(f"Creating {study_type} study with {setup_configuration} configuration")

    if await study_title_already_exists(study_title):
        raise Conflict("Study title already exists")

    study_id = str(uuid.uuid4()) 
    db: firestore.AsyncClient = current_app.config["DATABASE"]
    doc_ref = db.collection("studies").document(study_id)

    await doc_ref.set(
        {
            "study_id": study_id,
            "title": study_title,
            "study_type": study_type,
            "setup_configuration": setup_configuration,
            "private": private_study or demo,
            "demo": demo,
            "description": description,
            "study_information": study_information,
            "owner": user_id,
            "created": datetime.now(),
            "participants": ["Broad", user_id],
            "status": {"Broad": "ready to begin protocol", user_id: ""},
            "parameters": constants.SHARED_PARAMETERS[study_type],
            "advanced_parameters": constants.ADVANCED_PARAMETERS[study_type],
            "personal_parameters": {
                "Broad": constants.broad_user_parameters(),
                user_id: constants.default_user_parameters(study_type, demo),
            },
            "requested_participants": {},
            "invited_participants": [],
        }
    )

    await make_auth_key(study_id, "Broad")
    return jsonify({"message": "Study created successfully", "study_id": study_id})


@bp.route("/delete_study", methods=["DELETE"])
@authenticate
async def delete_study() -> Response:
    study_id = request.args.get("study_id")
    db: firestore.AsyncClient = current_app.config["DATABASE"]
    doc_ref = db.collection("studies").document(study_id)
    doc_ref_dict: dict = (await doc_ref.get()).to_dict()

    for participant in doc_ref_dict["personal_parameters"].values():
        if (gcp_project := participant.get("GCP_PROJECT").get("value")) != "":
            google_cloud_compute = GoogleCloudCompute(study_id, gcp_project)
            google_cloud_compute.delete_everything()

    logger.info("Successfully deleted GCP instances and other related resources")

    for participant in doc_ref_dict["personal_parameters"].values():
        if (auth_key := participant.get("AUTH_KEY").get("value")) != "":
            doc_ref_auth_keys = db.collection("users").document("auth_keys")
            await doc_ref_auth_keys.update({auth_key: firestore.DELETE_FIELD})

    await db.collection("deleted_studies").document(study_id).set(doc_ref_dict)

    await doc_ref.delete()

    return jsonify({"message": "Successfully deleted study"})


@bp.route("/study_information", methods=["POST"])
@authenticate
async def study_information() -> Response:
    try:
        study_id = request.args.get("study_id")
        data = await request.json
        description = data.get("description")
        study_information = data.get("information")

        doc_ref = (
            current_app.config["DATABASE"].collection("studies").document(study_id)
        )
        await doc_ref.set(
            {
                "description": description,
                "study_information": study_information,
            },
            merge=True,
        )

        return jsonify({"message": "Study information updated successfully"}), 200
    except Exception as e:
        logger.error(f"Failed to update study information: {e}")
        raise BadRequest()


@bp.route("/parameters", methods=["POST"])
@authenticate
async def parameters() -> Response:
    try:
        user_id = await get_user_id()
        study_id = request.args.get("study_id")
        data = await request.json
        db: firestore.AsyncClient = current_app.config["DATABASE"]
        doc_ref = db.collection("studies").document(study_id)
        doc_ref_dict = (await doc_ref.get()).to_dict()

        for p, value in data.items():
            if p in doc_ref_dict["parameters"]:
                doc_ref_dict["parameters"][p]["value"] = value
            elif p in doc_ref_dict["advanced_parameters"]:
                doc_ref_dict["advanced_parameters"][p]["value"] = value
            elif "NUM_INDS" in p:
                participant = p.split("NUM_INDS")[1]
                doc_ref_dict["personal_parameters"][participant]["NUM_INDS"][
                    "value"
                ] = value
            elif p in doc_ref_dict["personal_parameters"][user_id]:
                doc_ref_dict["personal_parameters"][user_id][p]["value"] = value
                if p == "NUM_CPUS":
                    doc_ref_dict["personal_parameters"][user_id]["NUM_THREADS"][
                        "value"
                    ] = value

        await doc_ref.set(doc_ref_dict, merge=True)

        return jsonify({"message": "Parameters updated successfully"}), 200
    except Exception as e:
        logger.error(f"Failed to update parameters: {e}")
        raise BadRequest()

@bp.route("/download_auth_key", methods=["GET"])
@authenticate
async def download_auth_key() -> Response:
    study_id = request.args.get("study_id")
    db: firestore.AsyncClient = current_app.config["DATABASE"]
    doc_ref = db.collection("studies").document(study_id)
    doc_ref_dict = (await doc_ref.get()).to_dict()
    user_id = await get_user_id()
    auth_key = doc_ref_dict["personal_parameters"][user_id]["AUTH_KEY"]["value"] or await make_auth_key(
        study_id, user_id
    )

    return await send_file(
        io.BytesIO(auth_key.encode()),
        attachment_filename="auth_key.txt",
        mimetype="text/plain",
        as_attachment=True,
    )