"""Archive orchestration kept separate from the Cloud Functions boundary."""


def run_archive(deps) -> tuple[dict, int]:
    """Run one bounded archive pass using composition-root dependencies."""
    deps._init_firebase()
    try:
        return {
            "pipeline_status": "ARCHIVE_OK",
            **deps.archive_terminal_records(deps.datetime.now(deps.UTC)),
        }, 200
    except Exception as exc:
        return {
            "pipeline_status": "ARCHIVE_ERROR",
            "error": deps._error_text(exc),
        }, 503
