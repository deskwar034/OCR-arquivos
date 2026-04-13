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


def slugify_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]+', "-", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name.rstrip(". ") or "arquivo"


def check_binary(name: str) -> str | None:
    return shutil.which(name)


def check_environment() -> tuple[bool, str]:
    missing = []

    for binary in ["tesseract", "ocrmypdf", "gs", "qpdf"]:
        if not check_binary(binary):
            missing.append(binary)

    if missing:
        return False, (
            "Dependências ausentes no sistema: " + ", ".join(missing) + ".\n\n"
            "No Streamlit Cloud, use também um arquivo packages.txt com:\n"
            "tesseract-ocr\n"
            "tesseract-ocr-por\n"
            "ghostscript\n"
            "qpdf"
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


def run_ocrmypdf(
    input_pdf: Path,
    output_pdf: Path,
    language: str = "por",
    deskew: bool = True,
    force_ocr: bool = False,
    skip_text: bool = True,
) -> None:
    cmd = [
        "ocrmypdf",
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
    ]

    if deskew:
        cmd.append("--deskew")

    if force_ocr:
        cmd.append("--force-ocr")
    elif skip_text:
        cmd.append("--skip-text")

    cmd.extend([str(input_pdf), str(output_pdf)])

    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"OCRmyPDF falhou em '{input_pdf.name}'.\n"
            f"STDOUT: {result.stdout[-1000:]}\n"
            f"STDERR: {result.stderr[-2000:]}"
        )


def process_zip_to_searchable_pdfs(
    zip_bytes: bytes,
    language: str = "por",
    deskew: bool = True,
    force_ocr: bool = False,
    skip_text: bool = True,
    progress_callback=None,
) -> tuple[bytes, list[dict]]:
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

        pdf_files = list(iter_pdf_files(extracted_dir))
        manifest: list[dict] = []

        total = len(pdf_files)
        if total == 0:
            raise RuntimeError("Nenhum arquivo PDF foi encontrado dentro do ZIP.")

        for idx, src in enumerate(pdf_files, start=1):
            rel = src.relative_to(extracted_dir)
            out_name = slugify_filename(str(rel).replace(os.sep, " - "))
            out_path = output_dir / out_name

            try:
                out_path.parent.mkdir(parents=True, exist_ok=True)
                run_ocrmypdf(
                    input_pdf=src,
                    output_pdf=out_path,
                    language=language,
                    deskew=deskew,
                    force_ocr=force_ocr,
                    skip_text=skip_text,
                )

                manifest.append(
                    {
                        "arquivo_origem": str(rel),
                        "arquivo_saida": out_path.name,
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

        manifest_txt = output_dir / "manifesto_processamento.csv"
        lines = ["arquivo_origem,arquivo_saida,status,erro"]
        for row in manifest:
            origem = row["arquivo_origem"].replace('"', '""')
            saida = row["arquivo_saida"].replace('"', '""')
            status = row["status"].replace('"', '""')
            erro = row["erro"].replace('"', '""').replace("\n", " ")
            lines.append(f'"{origem}","{saida}","{status}","{erro}"')
        manifest_txt.write_text("\n".join(lines), encoding="utf-8")

        readme = output_dir / "README.txt"
        readme.write_text(
            "\n".join(
                [
                    "Este ZIP contém os PDFs processados com OCR pesquisável.",
                    "Idioma do OCR: por",
                    "",
                    "Arquivos incluídos:",
                    "- PDFs OCRados",
                    "- manifesto_processamento.csv",
                ]
            ),
            encoding="utf-8",
        )

        result_zip = tmp / "resultado_pdfs_ocr.zip"
        with zipfile.ZipFile(result_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for file in sorted(output_dir.rglob("*")):
                if file.is_file():
                    zf.write(file, arcname=file.relative_to(output_dir))

        return result_zip.read_bytes(), manifest


st.set_page_config(
    page_title="OCR de PDFs pesquisáveis",
    page_icon="📄",
    layout="centered",
)

st.title("📄 OCR de PDFs pesquisáveis")
st.caption("Envie um arquivo .zip com PDFs e o app devolverá um novo .zip com os PDFs pesquisáveis.")

ok, env_msg = check_environment()
if ok:
    st.success(env_msg)
else:
    st.error(env_msg)
    st.stop()

with st.expander("Configurações", expanded=True):
    deskew = st.checkbox("Corrigir inclinação das páginas", value=True)
    mode = st.radio(
        "Modo de OCR",
        options=["Pular PDFs que já têm texto", "Forçar OCR em todos os PDFs"],
        index=0,
    )

    skip_text = mode == "Pular PDFs que já têm texto"
    force_ocr = mode == "Forçar OCR em todos os PDFs"

uploaded = st.file_uploader("Envie o arquivo .zip", type=["zip"])

if "result_zip_bytes" not in st.session_state:
    st.session_state.result_zip_bytes = None
if "manifest" not in st.session_state:
    st.session_state.manifest = []

if uploaded is not None:
    if st.button("Processar PDFs", type="primary", use_container_width=True):
        progress = st.progress(0, text="Iniciando...")
        status_box = st.empty()

        def update_progress(current: int, total: int, filename: str) -> None:
            fraction = current / total if total else 1.0
            progress.progress(fraction, text=f"Processando {current}/{total}")
            status_box.info(f"Arquivo atual: {filename}")

        try:
            result_zip_bytes, manifest = process_zip_to_searchable_pdfs(
                zip_bytes=uploaded.read(),
                language="por",
                deskew=deskew,
                force_ocr=force_ocr,
                skip_text=skip_text,
                progress_callback=update_progress,
            )
            st.session_state.result_zip_bytes = result_zip_bytes
            st.session_state.manifest = manifest
            progress.progress(1.0, text="Concluído")
            status_box.success("OCR finalizado com sucesso.")
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
            st.write(
                f"- {row['arquivo_origem']} | status={row['status']} | saída={row['arquivo_saida'] or '-'}"
            )
