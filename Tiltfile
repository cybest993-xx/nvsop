load("scripts/dev_tilt.star", "configure_manual_test_resources")

# 固定实例的服务图由 Compose 管理；Tilt 只负责可视化状态、日志和手动测试入口。
# scripts/dev.py 在 Tilt 外拥有快照、更新串行化和停止收尾，避免形成第二个进程管理器。
BASE_URL = os.getenv("NVSOP_DEV_BASE_URL", "https://localhost:8443")
MEDIA_URL = os.getenv("NVSOP_DEV_MEDIA_URL", "https://localhost:8444")
STATE_DIR = os.getenv("NVSOP_DEV_STATE_DIR", ".tmp/dev-main")
REPORTS_DIR = os.path.abspath(os.path.join(STATE_DIR, "reports"))
TARGET_SHA = os.getenv("NVSOP_TARGET_SHA", "local")
SMOKE_REPORT = "file://" + os.path.join(REPORTS_DIR, "smoke-" + TARGET_SHA + ".json")
UI_REPORTS = "file://" + os.path.join(REPORTS_DIR, "ui", TARGET_SHA)
docker_compose("deploy/dev/compose.yaml", project_name="nvsop-dev-main")

local_resource(
    "sample-data",
    cmd='python3 "$NVSOP_SOURCE_DIR/scripts/dev_seed.py" '
    + '--base-url "'
    + BASE_URL
    + '" '
    + '--login-name "$NVSOP_DEV_LOGIN_NAME" '
    + '--password-file "$NVSOP_DEV_STATE_DIR/secrets/bootstrap-password" '
    + '--video "$NVSOP_DEV_STATE_DIR/samples/dev-sample.mp4" '
    + '--report-file "$NVSOP_DEV_STATE_DIR/reports/sample-$NVSOP_TARGET_SHA.json"',
    deps=["scripts/dev_seed.py"],
    resource_deps=["gateway"],
    auto_init=True,
    labels=["development", "samples"],
    links=[
        BASE_URL + "/templates",
        BASE_URL + "/training-datasets",
        "file://" + os.path.join(REPORTS_DIR, "sample-" + TARGET_SHA + ".json"),
    ],
)

configure_manual_test_resources(
    base_url=BASE_URL,
    media_url=MEDIA_URL,
    smoke_report=SMOKE_REPORT,
    ui_reports=UI_REPORTS,
    host_launcher_script=os.getenv("NVSOP_HOST_LAUNCHER_SCRIPT", "scripts/dev.py"),
)
