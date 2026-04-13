#!/usr/bin/env python3
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Iterable

import streamlit as st


SUPPORTED_PDF_EXTS = {".pdf"}


def safe_name(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]+', "-", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name.rstrip(". ") or "arquivo.pdf"


def which(binary: str) -> str | None:
    return shutil.which(binary)


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


def run_ocrmypdf(input_pdf: Path, output_pdf: Path, language: str = "por") -> None:
    output_pdf.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
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
        str(input_pdf),
        str(output_pdf),
    ]

    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"OCRmyPDF falhou em '{input_pdf.name}'.\n"
            f"STDOUT:\n{result.stdout[-2000:]}\n\n"
            f"STDERR:\n{result.stderr[-4000:]}"
        )


def process_zip_to_searchable_pdfs(zip_bytes: bytes, progress_callback=None) -> tuple[bytes, list[dict]]:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        input_zip = tmp / "entrada.zip"
        extracted_dir = tmp / "extraido"
        output_dir = tmp / "saida"

        input_zip.write_bytes(zip_bytes)
        extracted_dir.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)

        with zipfile.ZipFile(input_zip, "r") as zf:
            zf.extractall(extracted_dir)

        pdf_files = sorted(iter_pdf_files(extracted_dir), key=lambda p: str(p).lower())
        if not pdf_files:
            raise RuntimeError("Nenhum arquivo PDF foi encontrado dentro do ZIP enviado.")

        manifest: list[dict] = []
        total = len(pdf_files)

        for idx, src in enumerate(pdf_files, start=1):
            rel = src.relative_to(extracted_dir)
            out_path = output_dir / rel

            try:
                run_ocrmypdf(src, out_path, language="por")
                manifest.append(
                    {
                        "arquivo_origem": str(rel),
                        "arquivo_saida": str(rel),
                        "status": "ok",
                        "erro": "",
                    }
                )
            except Exception as exc:
                manifest.append(
                    {
                        "arquivo_origem": str(rel),
                        "arquivo_saida": "",
                        "status": "erro",
                        "erro": str(exc),
                    }
                )

            if progress_callback:
                progress_callback(idx, total, str(rel))

        ok_count = sum(1 for row in manifest if row["status"] == "ok")
        if ok_count == 0:
            first_error = next((row["erro"] for row in manifest if row["status"] == "erro"), "Falha desconhecida.")
            raise RuntimeError(f"Nenhum PDF foi processado com sucesso.\n\n{first_error}")

        result_zip = tmp / "pdfs_ocr_pesquisaveis.zip"
        with zipfile.ZipFile(result_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for file in sorted(output_dir.rglob("*")):
                if file.is_file() and file.suffix.lower() == ".pdf":
                    zf.write(file, arcname=file.relative_to(output_dir))

        return result_zip.read_bytes(), manifest


st.set_page_config(
    page_title="OCR de PDFs pesquisáveis",
    page_icon="📄",
    layout="centered",
)

st.title("📄 OCR de PDFs pesquisáveis em ZIP")
st.caption(
    "Envie um arquivo .zip com PDFs. O app aplica OCR em português usando "
    "redo-ocr, preservando o texto nativo e tentando reconhecer texto em "
    "tabelas e imagens."
)

ok, env_msg = check_environment()
if ok:
    st.success(env_msg)
else:
    st.error(env_msg)
    st.stop()

uploaded = st.file_uploader("Envie o arquivo .zip", type=["zip"])

if "result_zip_bytes" not in st.session_state:
    st.session_state.result_zip_bytes = None
if "manifest" not in st.session_state:
    st.session_state.manifest = []

if uploaded is not None:
    st.info("O ZIP de saída conterá apenas os PDFs processados.")

    if st.button("Processar ZIP", type="primary", use_container_width=True):
        progress = st.progress(0, text="Iniciando...")
        status_box = st.empty()

        def update_progress(current: int, total: int, filename: str) -> None:
            fraction = current / total if total else 1.0
            progress.progress(fraction, text=f"Processando {current}/{total}")
            status_box.info(f"Arquivo atual: {filename}")

        try:
            result_zip_bytes, manifest = process_zip_to_searchable_pdfs(
                zip_bytes=uploaded.read(),
                progress_callback=update_progress,
            )
            st.session_state.result_zip_bytes = result_zip_bytes
            st.session_state.manifest = manifest
            progress.progress(1.0, text="Concluído")
            ok_count = sum(1 for row in manifest if row["status"] == "ok")
            err_count = sum(1 for row in manifest if row["status"] == "erro")
            status_box.success(
                f"Processamento finalizado. PDFs com sucesso: {ok_count}. Erros: {err_count}."
            )
        except Exception as exc:
            progress.empty()
            status_box.error(f"Erro durante o processamento: {exc}")

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

    with st.expander("Detalhes do processamento", expanded=False):
        for row in st.session_state.manifest:
            if row["status"] == "ok":
                st.write(f"- {row['arquivo_origem']} | status=ok")
            else:
                st.write(f"- {row['arquivo_origem']} | status=erro | {row['erro']}")
