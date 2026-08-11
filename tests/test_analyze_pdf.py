from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

import analyzePDF


class AnalyzePDFStateTests(unittest.TestCase):
    def test_error_message_removes_source_identity_paths_and_traceback(self) -> None:
        source = Path("/Users/alice/Documents/real-identity-123.pdf")
        message = (
            f"cannot parse {source}: cache /private/tmp/model.bin; "
            "mirror /Volumes/Shared/customer.pdf\n"
            "Traceback (most recent call last): secret"
        )

        sanitized = analyzePDF.sanitize_error_message(message, source)

        self.assertNotIn("alice", sanitized)
        self.assertNotIn("real-identity-123.pdf", sanitized)
        self.assertNotIn("/private/tmp", sanitized)
        self.assertNotIn("/Volumes/Shared", sanitized)
        self.assertNotIn("Traceback", sanitized)

    def test_skip_requires_matching_source_request_parser_and_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "sample.pdf"
            source.write_bytes(b"synthetic-pdf-bytes")
            output = root / "output"
            output.mkdir()
            (output / analyzePDF.MARKDOWN_FILENAME).write_text(
                "# synthetic\n", encoding="utf-8"
            )
            (output / analyzePDF.SOURCE_FILENAME).write_text(
                "sample.pdf\n", encoding="utf-8"
            )

            options = analyzePDF.OutputOptions.slim()
            source_hash = analyzePDF.file_sha256(source)
            started_at = analyzePDF.datetime.now(analyzePDF.timezone.utc)
            metadata = {
                "output_state_version": analyzePDF.OUTPUT_STATE_VERSION,
                "source": {
                    "content_sha256": source_hash,
                    "byte_size": source.stat().st_size,
                },
                "parser": {
                    "source_sha256": analyzePDF.file_sha256(
                        Path(analyzePDF.__file__).resolve()
                    )
                },
                "request": analyzePDF.output_request(False, options),
                "result": {
                    "status": "succeeded",
                    "started_at": started_at.isoformat(),
                    "completed_at": started_at.isoformat(),
                    "duration_ms": round(time.monotonic() - time.monotonic()),
                },
                "output_files": analyzePDF.output_inventory(output),
            }
            (output / analyzePDF.RUN_METADATA_FILENAME).write_text(
                json.dumps(metadata), encoding="utf-8"
            )

            self.assertTrue(
                analyzePDF.should_skip_existing(
                    source,
                    output,
                    use_ocr=False,
                    output_options=options,
                    source_sha256=source_hash,
                )
            )
            self.assertFalse(
                analyzePDF.should_skip_existing(
                    source,
                    output,
                    use_ocr=False,
                    output_options=analyzePDF.OutputOptions.full(),
                    source_sha256=source_hash,
                )
            )

            source.write_bytes(b"changed-pdf-bytes")
            self.assertFalse(
                analyzePDF.should_skip_existing(
                    source,
                    output,
                    use_ocr=False,
                    output_options=options,
                )
            )

    def test_missing_or_modified_output_is_not_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "sample.pdf"
            source.write_bytes(b"synthetic-pdf-bytes")
            output = root / "output"
            output.mkdir()
            markdown = output / analyzePDF.MARKDOWN_FILENAME
            markdown.write_text("# original\n", encoding="utf-8")
            options = analyzePDF.OutputOptions.slim()
            metadata = {
                "output_state_version": analyzePDF.OUTPUT_STATE_VERSION,
                "source": {
                    "content_sha256": analyzePDF.file_sha256(source),
                    "byte_size": source.stat().st_size,
                },
                "parser": {
                    "source_sha256": analyzePDF.file_sha256(
                        Path(analyzePDF.__file__).resolve()
                    )
                },
                "request": analyzePDF.output_request(False, options),
                "result": {"status": "succeeded"},
                "output_files": analyzePDF.output_inventory(output),
            }
            (output / analyzePDF.RUN_METADATA_FILENAME).write_text(
                json.dumps(metadata), encoding="utf-8"
            )

            markdown.write_text("# modified\n", encoding="utf-8")
            self.assertFalse(
                analyzePDF.should_skip_existing(
                    source,
                    output,
                    output_options=options,
                )
            )

    def test_cleanup_removes_only_analyzepdf_generated_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            for filename in (
                analyzePDF.MARKDOWN_FILENAME,
                analyzePDF.SOURCE_FILENAME,
                "content.json",
                analyzePDF.RUN_METADATA_FILENAME,
            ):
                (output / filename).write_text("generated", encoding="utf-8")
            for directory_name in (analyzePDF.ARTIFACTS_DIR_NAME, "tables"):
                generated_dir = output / directory_name
                generated_dir.mkdir()
                (generated_dir / "artifact.bin").write_bytes(b"generated")
            user_file = output / "review-notes.txt"
            user_file.write_text("keep", encoding="utf-8")

            inventoried_paths = {
                item["path"] for item in analyzePDF.output_inventory(output)
            }
            self.assertNotIn("review-notes.txt", inventoried_paths)

            analyzePDF.clear_previous_generated_outputs(output)

            self.assertTrue(user_file.is_file())
            self.assertEqual(user_file.read_text(encoding="utf-8"), "keep")
            self.assertFalse((output / analyzePDF.MARKDOWN_FILENAME).exists())
            self.assertFalse((output / analyzePDF.ARTIFACTS_DIR_NAME).exists())
            self.assertFalse((output / "tables").exists())

    def test_hf_endpoint_is_configured_before_library_import(self) -> None:
        env = os.environ.copy()
        env[analyzePDF.HF_MIRROR_ENV_VAR] = "https://example.invalid"
        env.pop(analyzePDF.USE_HF_MIRROR_ENV_VAR, None)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        command = (
            "import os; import analyzePDF; import huggingface_hub.constants as c; "
            "assert os.environ.get('HF_ENDPOINT') is None; "
            "assert c.ENDPOINT != 'https://example.invalid'"
        )

        result = subprocess.run(
            [sys.executable, "-c", command],
            cwd=Path(analyzePDF.__file__).resolve().parent,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_failed_parse_replaces_stale_outputs_with_sanitized_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "real-identity-123.pdf"
            source.write_bytes(b"not-a-real-pdf")
            output_root = root / "output"
            output = analyzePDF.resolve_output_dir(source, output_root)
            output.mkdir(parents=True)
            (output / analyzePDF.MARKDOWN_FILENAME).write_text(
                "# stale success\n", encoding="utf-8"
            )
            user_notes = output / "review-notes.txt"
            user_notes.write_text("keep", encoding="utf-8")
            errors = [
                analyzePDF.classify_parse_error(
                    RuntimeError(f"failed at {source}"), source, "docling-parse"
                )
            ]

            with mock.patch.object(
                analyzePDF,
                "_convert_pdf_with_backend",
                side_effect=analyzePDF.PDFConversionFailure(errors),
            ):
                with self.assertRaises(analyzePDF.PDFConversionFailure):
                    analyzePDF.parse_pdf(source, output_root=output_root)

            run = json.loads(
                (output / analyzePDF.RUN_METADATA_FILENAME).read_text(encoding="utf-8")
            )
            serialized = json.dumps(run, ensure_ascii=False)
            self.assertEqual(run["result"]["status"], "failed")
            self.assertEqual(run["output_files"], [])
            self.assertNotIn(source.name, serialized)
            self.assertNotIn(str(source), serialized)
            self.assertFalse((output / analyzePDF.MARKDOWN_FILENAME).exists())
            self.assertEqual(user_notes.read_text(encoding="utf-8"), "keep")

    def test_complete_failure_falls_back_to_usable_partial_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "sample.pdf"
            source.write_bytes(b"synthetic")
            partial = SimpleNamespace(
                status=analyzePDF.ConversionStatus.PARTIAL_SUCCESS,
                document=object(),
                errors=[RuntimeError(f"one page failed at {source}")],
            )
            failed = SimpleNamespace(
                status=analyzePDF.ConversionStatus.FAILURE,
                document=None,
                errors=[RuntimeError("fallback could not recover the page")],
            )

            class FakeConverter:
                def __init__(self, result):
                    self.result = result

                def convert(self, _input_path):
                    return self.result

            with mock.patch.object(
                analyzePDF, "create_converter", return_value=FakeConverter(failed)
            ):
                outcome = analyzePDF._convert_pdf_with_backend(
                    source,
                    use_ocr=False,
                    converter=FakeConverter(partial),
                )

            self.assertIs(outcome.result, partial)
            self.assertEqual(outcome.backend, "docling-parse")
            self.assertGreaterEqual(len(outcome.errors), 2)
            serialized = json.dumps(outcome.errors, ensure_ascii=False)
            self.assertNotIn(source.name, serialized)
            self.assertNotIn(str(source), serialized)

    def test_table_export_error_publishes_partial_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "sample.pdf"
            source.write_bytes(b"synthetic")
            output = root / "output"

            class FailingTable:
                def export_to_dataframe(self, doc):
                    raise RuntimeError(f"temporary export failure for {source}")

            class FakeDocument:
                tables = [FailingTable()]

                def save_as_markdown(self, path, **_kwargs):
                    Path(path).write_text("# usable text\n", encoding="utf-8")

                def export_to_dict(self):
                    return {"schema_name": "synthetic"}

            analyzePDF.write_outputs(
                document=FakeDocument(),
                input_path=source,
                output_path=output,
                options=analyzePDF.OutputOptions.full(),
                source_sha256=analyzePDF.file_sha256(source),
                use_ocr=False,
                backend="docling-parse",
                conversion_errors=(),
                started_at=analyzePDF.datetime.now(analyzePDF.timezone.utc),
                started_monotonic=time.monotonic(),
            )

            run = json.loads(
                (output / analyzePDF.RUN_METADATA_FILENAME).read_text(encoding="utf-8")
            )
            self.assertEqual(run["result"]["status"], "partial")
            self.assertEqual(run["errors"][0]["code"], "TABLE_EXPORT_FAILED")
            self.assertNotIn(source.name, json.dumps(run["errors"], ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
