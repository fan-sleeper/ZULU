import subprocess
import tempfile
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from core.management.commands.import_alerts import Command as ImportAlertsCommand


class Command(BaseCommand):
    help = "Fetch latest ProMED alerts in one spider run and import them"

    def add_arguments(self, parser):
        parser.add_argument(
            "--max-pages",
            type=int,
            default=10,
            help="Maximum number of pages to fetch in a single spider run",
        )
        parser.add_argument(
            "--start-page",
            type=int,
            default=1,
            help="Page number to start from",
        )
        parser.add_argument(
            "--mode",
            type=str,
            default="incremental",
            choices=["incremental", "backfill"],
            help="Run either an incremental sync or a historical backfill",
        )

    def handle(self, *args, **options):
        scraper_dir = Path(__file__).resolve().parents[3] / "scraper"

        start_page = options["start_page"]
        max_pages = options["max_pages"]
        mode = options["mode"]

        with tempfile.TemporaryDirectory() as tmpdir:
            output_file = Path(tmpdir) / "promed_sync.json"

            cmd = [
                "scrapy",
                "crawl",
                "example",
                "-a",
                f"mode={mode}",
                "-a",
                f"start_page={start_page}",
                "-a",
                f"max_pages={max_pages}",
                "-O",
                str(output_file),
            ]

            self.stdout.write(
                f"Running ProMED sync in one spider process "
                f"(start_page={start_page}, max_pages={max_pages})..."
            )

            process = subprocess.Popen(
                cmd,
                cwd=scraper_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )

            captured_output = []

            assert process.stdout is not None
            for line in process.stdout:
                self.stdout.write(line.rstrip())
                captured_output.append(line)

            return_code = process.wait()

            if return_code != 0:
                raise CommandError(
                    "Spider failed.\n"
                    f"STDOUT:\n{''.join(captured_output)}"
                )

            if not output_file.exists():
                raise CommandError("Spider finished but no output file was created.")

            summary = ImportAlertsCommand().handle(file=str(output_file))
            created = summary.get("created", 0)
            skipped = summary.get("skipped", 0)

            self.stdout.write(
                self.style.SUCCESS(
                    f"Sync complete. Created={created}, skipped={skipped}"
                )
            )