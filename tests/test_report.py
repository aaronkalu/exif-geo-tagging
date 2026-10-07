from pathlib import Path

from exif_geotag.geotagger import Issue, Outcome, Result
from exif_geotag.report import RunReport


def result(name: str, outcome: Outcome, *issues: Issue) -> Result:
    return Result(Path(name), outcome, "", issues)


def test_clean_run_has_no_warnings() -> None:
    report = RunReport()
    report.add(result("a.jpg", Outcome.TAGGED))
    report.add(result("b.jpg", Outcome.SKIPPED))

    assert report.render() == "Done. 1 tagged, 1 skipped, 0 failed."
    assert not report.has_failures


def test_warnings_are_grouped_by_issue() -> None:
    report = RunReport(malformed_timeline_entries=2)
    report.add(result("b.jpg", Outcome.TAGGED, Issue.MISSING_TIMEZONE))
    report.add(result("a.jpg", Outcome.SKIPPED, Issue.MISSING_TIMEZONE, Issue.NO_NEARBY_LOCATION))
    report.add(result("c.jpg", Outcome.FAILED, Issue.EXIFTOOL_ERROR))

    assert report.render().splitlines() == [
        "Done. 1 tagged, 1 skipped, 1 failed.",
        "",
        "Warnings:",
        "  - 2 malformed timeline entries ignored",
        f"  - 1 image: {Issue.NO_NEARBY_LOCATION.value}",
        "      a.jpg",
        f"  - 2 images: {Issue.MISSING_TIMEZONE.value}",
        "      a.jpg",
        "      b.jpg",
        f"  - 1 image: {Issue.EXIFTOOL_ERROR.value}",
        "      c.jpg",
    ]
    assert report.has_failures


def test_long_image_lists_are_truncated() -> None:
    report = RunReport()
    for index in range(7):
        report.add(result(f"{index}.jpg", Outcome.SKIPPED, Issue.MISSING_CAPTURE_TIME))

    lines = report.render().splitlines()
    assert lines[-2:] == ["      4.jpg", "      ... and 2 more"]


def test_warns_when_no_images_found() -> None:
    assert "  - no supported images or videos found" in RunReport().render().splitlines()


def test_dry_run_and_interrupted_headings() -> None:
    assert RunReport(dry_run=True).render().startswith("Dry run, no files were changed. Would have: 0 tagged")
    report = RunReport()
    report.interrupted = True
    assert report.render() == "Interrupted, processed so far: 0 tagged, 0 skipped, 0 failed."
