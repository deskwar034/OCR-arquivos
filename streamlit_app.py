#!/usr/bin/env python3
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from collections import deque
from pathlib import Path
from typing import Iterable


import streamlit as st


SUPPORTED_PDF_EXTS = {".pdf"}


def which(binary: str) -> str | None:
    return shutil.which(binary)


def format_seconds(seconds: float | None) -> str:
    if seconds is None:
        return "calculando..."
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h}h {m:02d}m {s:02d}s"
    if m > 0:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def check_environment() -> tuple[bool, str]:
    missing = []

    for binary in ["tesseract", "ocrmypdf", "gs", "qpdf"]:
        if not which(binary):
            missing.append(binary)

    if missing:
        return False, (
            "Dependências ausentes no sistema: " + ", ".join(missing) + ".\n\n"
            "No Streamlit Cloud, use packages.txt com:\n"
            "tesseract-ocr\n"
            "tesseract-ocr-por\n"
            "ghostscript\n"
            "qpdf\n"
            "pngquant\n"
            "unpaper"
        )

    try:
        out = subprocess.check_output(
            ["tesseract", "--list-langs"],
            text=True,
            stderr=subprocess.STDOUT,
        )
        langs = {line.strip() for line in out.splitlines() if line.strip()}
    except Exception as exc:
        return False, f"Não foi possível verificar os idiomas do Tesseract: {exc}"

    if "por" not in langs:
        return False, (
            "O idioma 'por' não está instalado no Tesseract.\n\n"
            "No Streamlit Cloud, adicione em packages.txt:\n"
            "tesseract-ocr\n"
            "tesseract-ocr-por"
        )

    return True, "Ambiente OCR pronto."


def iter_pdf_files(root: Path) -> Iterable[Path]:
    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() in SUPPORTED_PDF_EXTS:
            yield path


def trim_log(lines: list[str], max_lines: int = 800) -> list[str]:
    if len(lines) <= max_lines:
        return lines
    return lines[-max_lines:]


def build_ocr_command(input_pdf: Path, output_pdf: Path, language: str = "por") -> list[str]:
    return [
        "ocrmypdf",
        "--redo-ocr",
        "--language",
        language,
        "--output-type",
        "pdf",
        "--optimize",
        "0",
        "--jobs",
        "1",
        "--fast-web-view",
        "0",
        "--tesseract-timeout",
        "180",
        "--verbose",
        "1",
        str(input_pdf),
        str(output_pdf),
    ]


def extract_page_progress(line: str) -> tuple[int, int] | None:
    """
    Tenta detectar progresso por página em linhas do OCRmyPDF/Tesseract.
    Exemplos que queremos capturar:
    - "page 3 of 12"
    - "Page 3/12"
    """
    patterns = [
        r"[Pp]age\s+(\d+)\s+of\s+(\d+)",
        r"[Pp]age\s+(\d+)\s*/\s*(\d+)",
    ]
    for pattern in patterns:
        m = re.search(pattern, line)
        if m:
            current = int(m.group(1))
            total = int(m.group(2))
            if total > 0 and 1 <= current <= total:
                return current, total
    return None


class UILogger:
    def __init__(self):
        self.progress = st.progress(0, text="Aguardando início...")
        self.current_file_box = st.empty()
        self.summary_box = st.empty()
        self.metrics_box = st.empty()
        self.log_box = st.empty()

        self.log_lines: list[str] = []
        self.completed_times: list[float] = []
        self.recent_times: deque[float] = deque(maxlen=5)

        self.batch_start: float | None = None
        self.current_start: float | None = None
        self.current_idx: int = 0
        self.total_files: int = 0
        self.current_file: str = ""

        self.current_page: int | None = None
        self.current_total_pages: int | None = None

    def start_batch(self, total_files: int) -> None:
        self.batch_start = time.time()
        self.total_files = total_files
        self._render_metrics()

    def start_file(self, filename: str, idx: int, total: int) -> None:
        self.current_start = time.time()
        self.current_idx = idx
        self.total_files = total
        self.current_file = filename
        self.current_page = None
        self.current_total_pages = None
        self.current_file_box.info(f"Arquivo atual: {filename} ({idx}/{total})")
        self._render_metrics()

    def finish_file(self, elapsed: float) -> None:
        self.completed_times.append(elapsed)
        self.recent_times.append(elapsed)
        self._render_metrics()

    def add_log(self, message: str) -> None:
        ts = time.strftime("%H:%M:%S")
        self.log_lines.append(f"[{ts}] {message}")
        self.log_lines = trim_log(self.log_lines, max_lines=800)

        page_progress = extract_page_progress(message)
        if page_progress:
            self.current_page, self.current_total_pages = page_progress

        self.log_box.code("\n".join(self.log_lines), language="text")
        self._render_metrics()

    def set_progress(self, fraction: float, text: str) -> None:
        self.progress.progress(max(0.0, min(1.0, fraction)), text=text)

    def update_summary(self, manifest: list[dict], total: int) -> None:
        ok_count = sum(1 for row in manifest if row["status"] == "ok")
        err_count = sum(1 for row in manifest if row["status"] == "erro")
        done = len(manifest)
        self.summary_box.markdown(
            f"**Resumo:** {done}/{total} concluídos • "
            f"**Sucesso:** {ok_count} • **Erros:** {err_count}"
        )
        self._render_metrics()

    def estimate_current_file_remaining(self) -> float | None:
        if self.current_start is None:
            return None

        elapsed = time.time() - self.current_start

        if self.current_page and self.current_total_pages and self.current_page > 0:
            avg_per_page = elapsed / self.current_page
            remaining_pages = max(0, self.current_total_pages - self.current_page)
            return avg_per_page * remaining_pages

        if self.recent_times:
            return sum(self.recent_times) / len(self.recent_times)

        return None

    def estimate_batch_remaining(self) -> float | None:
        done_files = len(self.completed_times)

        current_remaining = self.estimate_current_file_remaining()

        if done_files > 0:
            avg = sum(self.recent_times) / len(self.recent_times) if self.recent_times else sum(self.completed_times) / len(self.completed_times)
            remaining_after_current = max(0, self.total_files - self.current_idx) * avg
            if current_remaining is None:
                current_remaining = avg
            return current_remaining + remaining_after_current

        return current_remaining

    def _render_metrics(self) -> None:
        batch_elapsed = (time.time() - self.batch_start) if self.batch_start else None
        current_elapsed = (time.time() - self.current_start) if self.current_start else None
        current_remaining = self.estimate_current_file_remaining()
        batch_remaining = self.estimate_batch_remaining()

        avg_file = None
        if self.completed_times:
            avg_file = sum(self.completed_times) / len(self.completed_times)

        recent_avg = None
        if self.recent_times:
            recent_avg = sum(self.recent_times) / len(self.recent_times)

        page_txt = "-"
        if self.current_page and self.current_total_pages:
            page_txt = f"{self.current_page}/{self.current_total_pages}"

        self.metrics_box.markdown(
            "\n".join(
                [
                    f"**Tempo decorrido total:** {format_seconds(batch_elapsed)}",
                    f"**Tempo do arquivo atual:** {format_seconds(current_elapsed)}",
                    f"**ETA do arquivo atual:** {format_seconds(current_remaining)}",
                    f"**ETA do lote inteiro:** {format_seconds(batch_remaining)}",
                    f"**Média por arquivo:** {format_seconds(avg_file)}",
                    f"**Média móvel (últimos 5):** {format_seconds(recent_avg)}",
                    f"**Progresso interno do arquivo:** {page_txt}",
                ]
            )
        )


def run_ocrmypdf_streaming(
    input_pdf: Path,
    output_pdf: Path,
    log_callback,
    language: str = "por",
) -> None:
    output_pdf.parent.mkdir(parents=True, exist_ok=True)

    cmd = build_ocr_command(input_pdf, output_pdf, language=language)

    log_callback(f"$ {' '.join(cmd)}")
    log_callback(f"Iniciando OCR: {input_pdf.name}")

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        universal_newlines=True,
    )

    assert proc.stdout is not None

    for raw_line in proc.stdout:
        line = raw_line.rstrip()
        if line:
            log_callback(line)

    return_code = proc.wait()

    if return_code != 0:
        raise RuntimeError(f"OCRmyPDF retornou código {return_code} para '{input_pdf.name}'.")

    if not output_pdf.exists():
        raise RuntimeError(f"Arquivo de saída não foi criado para '{input_pdf.name}'.")

    log_callback(f"Concluído: {input_pdf.name}")


def process_zip_to_searchable_pdfs(
    zip_bytes: bytes,
    continue_on_error: bool,
    ui: UILogger,
) -> tuple[bytes, list[dict]]:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        input_zip = tmp / "entrada.zip"
        extracted_dir = tmp / "extraido"
        output_dir = tmp / "saida"

        input_zip.write_bytes(zip_bytes)
        extracted_dir.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)

        ui.add_log("Abrindo ZIP enviado...")
        with zipfile.ZipFile(input_zip, "r") as zf:
            zf.extractall(extracted_dir)

        pdf_files = sorted(iter_pdf_files(extracted_dir), key=lambda p: str(p).lower())
        if not pdf_files:
            raise RuntimeError("Nenhum arquivo PDF foi encontrado dentro do ZIP enviado.")

        ui.add_log(f"PDFs encontrados: {len(pdf_files)}")
        ui.start_batch(len(pdf_files))

        manifest: list[dict] = []
        total = len(pdf_files)

        for idx, src in enumerate(pdf_files, start=1):
            rel = src.relative_to(extracted_dir)
            out_path = output_dir / rel
            started = time.time()

            ui.start_file(str(rel), idx, total)
            ui.add_log("")
            ui.add_log("=" * 80)
            ui.add_log(f"[{idx}/{total}] Processando: {rel}")
            ui.add_log("=" * 80)

            try:
                run_ocrmypdf_streaming(
                    src,
                    out_path,
                    log_callback=ui.add_log,
                    language="por",
                )

                elapsed = time.time() - started
                manifest.append(
                    {
                        "arquivo_origem": str(rel),
                        "arquivo_saida": str(rel),
                        "status": "ok",
                        "erro": "",
                        "segundos": round(elapsed, 2),
                    }
                )
                ui.finish_file(elapsed)
                ui.add_log(f"Sucesso em {elapsed:.1f}s: {rel}")
            except Exception as exc:
                elapsed = time.time() - started
                manifest.append(
                    {
                        "arquivo_origem": str(rel),
                        "arquivo_saida": "",
                        "status": "erro",
                        "erro": str(exc),
                        "segundos": round(elapsed, 2),
                    }
                )
                ui.finish_file(elapsed)
                ui.add_log(f"ERRO em {elapsed:.1f}s: {rel}")
                ui.add_log(str(exc))

                if not continue_on_error:
                    raise

            batch_remaining = ui.estimate_batch_remaining()
            ui.set_progress(
                fraction=idx / total,
                text=f"Processando {idx}/{total} • ETA lote: {format_seconds(batch_remaining)}"
            )
            ui.update_summary(manifest, total)

        ok_count = sum(1 for row in manifest if row["status"] == "ok")
        if ok_count == 0:
            first_error = next((row["erro"] for row in manifest if row["status"] == "erro"), "Falha desconhecida.")
            raise RuntimeError(f"Nenhum PDF foi processado com sucesso.\n\n{first_error}")

        result_zip = tmp / "pdfs_ocr_pesquisaveis.zip"
        ui.add_log("")
        ui.add_log("Compactando PDFs processados...")

        with zipfile.ZipFile(result_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for file in sorted(output_dir.rglob("*")):
                if file.is_file() and file.suffix.lower() == ".pdf":
                    zf.write(file, arcname=file.relative_to(output_dir))

        ui.add_log(f"ZIP final criado: {result_zip.name}")
        return result_zip.read_bytes(), manifest


st.set_page_config(
    page_title="OCR de PDFs pesquisáveis",
    page_icon="📄",
    layout="centered",
)

st.title("📄 OCR de PDFs pesquisáveis em ZIP")
st.caption(
    "Envie um arquivo .zip com PDFs. O app aplica OCR em português usando redo-ocr, "
    "preservando o texto nativo e tentando reconhecer texto em tabelas e imagens."
)

ok, env_msg = check_environment()
if ok:
    st.success(env_msg)
else:
    st.error(env_msg)
    st.stop()

with st.expander("Configurações", expanded=True):
    continue_on_error = st.checkbox(
        "Continuar processando mesmo se um PDF falhar",
        value=True,
    )

uploaded = st.file_uploader("Envie o arquivo .zip", type=["zip"])

if "result_zip_bytes" not in st.session_state:
    st.session_state.result_zip_bytes = None
if "manifest" not in st.session_state:
    st.session_state.manifest = []

if uploaded is not None:
    st.info("O ZIP de saída conterá apenas os PDFs processados.")

    if st.button("Processar ZIP", type="primary", use_container_width=True):
        ui = UILogger()
        ui.add_log("Recebido arquivo ZIP do usuário.")
        ui.add_log(f"Nome: {uploaded.name}")
        ui.add_log(f"Tamanho: {uploaded.size / (1024 * 1024):.2f} MB")

        try:
            result_zip_bytes, manifest = process_zip_to_searchable_pdfs(
                zip_bytes=uploaded.read(),
                continue_on_error=continue_on_error,
                ui=ui,
            )
            st.session_state.result_zip_bytes = result_zip_bytes
            st.session_state.manifest = manifest

            ok_count = sum(1 for row in manifest if row["status"] == "ok")
            err_count = sum(1 for row in manifest if row["status"] == "erro")

            ui.set_progress(1.0, f"Concluído • sucesso: {ok_count} • erros: {err_count}")
            ui.add_log("")
            ui.add_log("Processamento finalizado.")
        except Exception as exc:
            ui.add_log("")
            ui.add_log(f"FALHA GERAL: {exc}")
            st.error(f"Erro durante o processamento: {exc}")

if st.session_state.result_zip_bytes:
    st.download_button(
        label="📦 Baixar ZIP com PDFs pesquisáveis",
        data=st.session_state.result_zip_bytes,
        file_name="pdfs_pesquisaveis_ocr.zip",
        mime="application/zip",
        use_container_width=True,
    )

    ok_count = sum(1 for row in st.session_state.manifest if row["status"] == "ok")
    err_count = sum(1 for row in st.session_state.manifest if row["status"] == "erro")

    st.write(f"Processados com sucesso: **{ok_count}**")
    st.write(f"Com erro: **{err_count}**")

    with st.expander("Detalhes por arquivo", expanded=False):
        for row in st.session_state.manifest:
            if row["status"] == "ok":
                st.write(
                    f"- {row['arquivo_origem']} | status=ok | tempo={row['segundos']}s"
                )
            else:
                st.write(
                    f"- {row['arquivo_origem']} | status=erro | tempo={row['segundos']}s | {row['erro']}"
                )
