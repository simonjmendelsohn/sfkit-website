from datetime import datetime

from flask import (
    Blueprint,
    current_app,
    g,
    make_response,
    redirect,
    render_template,
    request,
    url_for,
)
from werkzeug import Response

from src.utils import constants
from src.auth import login_required
from src.utils.generic_functions import redirect_with_flash
from src.utils.google_cloud.google_cloud_compute import GoogleCloudCompute
from src.utils.google_cloud.google_cloud_storage import GoogleCloudStorage
from src.utils.gwas_functions import valid_study_title

bp = Blueprint("studies", __name__)


@bp.route("/index")
def index() -> Response:
    db = current_app.config["DATABASE"]
    studies = db.collection("studies")
    studies_list = [study.to_dict() for study in studies.stream()]
    return make_response(render_template("studies/index.html", studies=studies_list))


@bp.route("/study/<study_title>", methods=("GET", "POST"))
@login_required
def study(study_title: str) -> Response:
    db = current_app.config["DATABASE"]
    doc_ref = db.collection("studies").document(study_title.replace(" ", "").lower())
    doc_ref_dict = doc_ref.get().to_dict()
    public_keys = [
        doc_ref_dict["personal_parameters"][user]["PUBLIC_KEY"]["value"]
        for user in doc_ref_dict["participants"]
    ]
    id = g.user["id"]
    role: int = doc_ref_dict["participants"].index(id) + 1

    return make_response(
        render_template(
            "studies/study.html",
            study=doc_ref_dict,
            public_keys=public_keys,
            role=role,
            parameters=doc_ref_dict["personal_parameters"][id],
        )
    )


@bp.route("/create_study", methods=("GET", "POST"))
@login_required
def create_study() -> Response:
    if request.method == "GET":
        return make_response(render_template("studies/create_study.html"))

    db = current_app.config["DATABASE"]
    title = request.form["title"]
    description = request.form["description"]
    study_information = request.form["study_information"]

    (valid, response) = valid_study_title(title)
    if not valid:
        return response

    doc_ref = db.collection("studies").document(title.replace(" ", "").lower())
    doc_ref.set(
        {
            "title": title,
            "description": description,
            "study_information": study_information,
            "owner": g.user["id"],
            "created": datetime.now(),
            "participants": [g.user["id"]],
            "status": {g.user["id"]: [""]},
            "parameters": constants.DEFAULT_SHARED_PARAMETERS,
            "personal_parameters": {g.user["id"]: constants.DEFAULT_USER_PARAMETERS},
            "requested_participants": [],
        }
    )
    return response


@bp.route("/delete_study/<study_title>", methods=("POST",))
@login_required
def delete_study(study_title: str) -> Response:
    db = current_app.config["DATABASE"]
    doc_ref = db.collection("studies").document(study_title.replace(" ", "").lower())
    doc_ref_dict = doc_ref.get().to_dict()

    # delete vms that may still exist
    google_cloud_compute = GoogleCloudCompute(
        ""
    )  # TODO: delete the server's VM as well
    for participant in doc_ref_dict["personal_parameters"].values():
        if (gcp_project := participant.get("GCP_PROJECT").get("value")) != "":
            google_cloud_compute.project = gcp_project
            for instance in google_cloud_compute.list_instances():
                if constants.INSTANCE_NAME_ROOT in instance:
                    google_cloud_compute.delete_instance(instance)

    doc_ref.delete()
    return redirect(url_for("studies.index"))


@bp.route("/request_join_study/<study_title>")
@login_required
def request_join_study(study_title: str) -> Response:
    db = current_app.config["DATABASE"]
    doc_ref = db.collection("studies").document(study_title.replace(" ", "").lower())
    doc_ref_dict = doc_ref.get().to_dict()
    doc_ref_dict["requested_participants"] = [g.user["id"]]
    doc_ref.set(
        {"requested_participants": doc_ref_dict["requested_participants"]},
        merge=True,
    )
    return redirect(url_for("studies.index"))


@bp.route("/approve_join_study/<study_title>/<user_id>")
def approve_join_study(study_title: str, user_id: str) -> Response:
    db = current_app.config["DATABASE"]
    doc_ref = db.collection("studies").document(study_title.replace(" ", "").lower())
    doc_ref_dict = doc_ref.get().to_dict()

    doc_ref.set(
        {
            "requested_participants": doc_ref_dict["requested_participants"].remove(
                user_id
            ),
            "participants": doc_ref_dict["participants"] + [user_id],
            "personal_parameters": doc_ref_dict["personal_parameters"]
            | {user_id: constants.DEFAULT_USER_PARAMETERS},
            "status": doc_ref_dict["status"] | {user_id: [""]},
        },
        merge=True,
    )

    return redirect(url_for("studies.study", study_title=study_title))


@bp.route("/parameters/<study_title>", methods=("GET", "POST"))
@login_required
def parameters(study_title: str) -> Response:
    gcloudStorage = GoogleCloudStorage(constants.SERVER_GCP_PROJECT)

    db = current_app.config["DATABASE"]
    doc_ref = db.collection("studies").document(study_title.replace(" ", "").lower())
    parameters = doc_ref.get().to_dict().get("parameters")
    pos_file_uploaded = gcloudStorage.check_file_exists("pos.txt")
    if request.method == "GET":
        return make_response(
            render_template(
                "studies/parameters.html",
                study_title=study_title,
                parameters=parameters,
                pos_file_uploaded=pos_file_uploaded,
            )
        )
    elif "save" in request.form:
        for p in parameters["index"]:
            parameters[p]["value"] = request.form.get(p)
        doc_ref.set({"parameters": parameters}, merge=True)
        return redirect(url_for("studies.study", study_title=study_title))
    elif "upload" in request.form:
        file = request.files["file"]
        if file.filename == "":
            return redirect_with_flash(
                url=url_for("studies.parameters", study_title=study_title),
                message="Please select a file to upload.",
            )
        elif file and file.filename == "pos.txt":
            gcloudStorage.upload_to_bucket(file, file.filename)
            return redirect(url_for("studies.study", study_title=study_title))
        else:
            return redirect_with_flash(
                url=url_for("studies.parameters", study_title=study_title),
                message="Please upload a valid pos.txt file.",
            )
    else:
        return redirect_with_flash(
            url=url_for("studies.parameters", study_title=study_title),
            message="Something went wrong. Please try again.",
        )


@bp.route("/personal_parameters/<study_title>", methods=("GET", "POST"))
def personal_parameters(study_title):
    db = current_app.config["DATABASE"]
    doc_ref = db.collection("studies").document(study_title.replace(" ", "").lower())
    parameters = doc_ref.get().to_dict().get("personal_parameters")

    if request.method == "GET":
        return render_template(
            "studies/personal_parameters.html",
            study_title=study_title,
            parameters=parameters[g.user["id"]],
        )

    for p in parameters[g.user["id"]]["index"]:
        if p in request.form:
            parameters[g.user["id"]][p]["value"] = request.form.get(p)
    doc_ref.set({"personal_parameters": parameters}, merge=True)
    return redirect(url_for("studies.study", study_title=study_title))