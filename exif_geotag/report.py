from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path

from exif_geotag.geotagger import Issue, Outcome, Result

MAX_LISTED_IMAGES = 5


class RunReport:
    def __init__(self, malformed_timeline_entries: int = 0, dry_run: bool = False, interrupted: bool = False) -> None:
        self._malformed_timeline_entries = malformed_timeline_entries
        self._dry_run = dry_run
        self.interrupted = interrupted
        self._outcomes: Counter[Outcome] = Counter()
        self._images_by_issue: dict[Issue, list[Path]] = defaultdict(list)

    def add(self, result: Result) -> None:
        self._outcomes[result.outcome] += 1
        for issue in result.issues:
            self._images_by_issue[issue].append(result.image)

    @property
    def has_failures(self) -> bool:
        return self._outcomes[Outcome.FAILED] > 0

    def render(self) -> str:
        counts = ", ".join(f"{self._outcomes[outcome]} {outcome.value}" for outcome in Outcome)
        if self.interrupted:
            heading = "Interrupted, processed so far:"
        elif self._dry_run:
            heading = "Dry run, no files were changed. Would have:"
        else:
            heading = "Done."
        lines = [f"{heading} {counts}."]

        warnings = self._warning_lines()
        if warnings:
            lines += ["", "Warnings:", *warnings]
        return "\n".join(lines)

    def _warning_lines(self) -> list[str]:
        lines = []
        if not self._outcomes and not self.interrupted:
            lines.append("  - no supported images or videos found")
        if self._malformed_timeline_entries:
            count = self._malformed_timeline_entries
            lines.append(f"  - {count} malformed timeline {'entry' if count == 1 else 'entries'} ignored")

        for issue in Issue:
            images = sorted(self._images_by_issue.get(issue, []))
            if not images:
                continue
            noun = "image" if len(images) == 1 else "images"
            lines.append(f"  - {len(images)} {noun}: {issue.value}")
            lines += [f"      {image}" for image in images[:MAX_LISTED_IMAGES]]
            if len(images) > MAX_LISTED_IMAGES:
                lines.append(f"      ... and {len(images) - MAX_LISTED_IMAGES} more")
        return lines
