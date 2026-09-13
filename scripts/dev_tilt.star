def configure_manual_test_resources(base_url, media_url, smoke_report, ui_reports, host_launcher_script):
    local_resource(
        "functional-smoke",
        cmd='python3 "' + host_launcher_script + '" smoke',
        deps=["scripts/dev_smoke.py", "scripts/dev.py"],
        resource_deps=["sample-data"],
        trigger_mode=TRIGGER_MODE_MANUAL,
        auto_init=False,
        labels=["development", "tests"],
        links=[base_url, media_url, smoke_report],
    )

    local_resource(
        "visual-tests",
        cmd='python3 "' + host_launcher_script + '" test-ui',
        deps=["apps/control-web/playwright.config.ts", "scripts/dev.py"],
        resource_deps=["gateway"],
        trigger_mode=TRIGGER_MODE_MANUAL,
        auto_init=False,
        labels=["development", "tests"],
        links=["http://localhost:9323", ui_reports],
    )
