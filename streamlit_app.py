#!/usr/bin/env python3
from __future__ import annotations

import csv
import io
import os
import re
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Iterable, List, Tuple

import fitz  # PyMuPDF
from PIL import Image
import pytesseract
import streamlit as st


SUPPORTED_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}
SUPPORTED_PDF_EXTS = {".pdf"}


def slugify_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]+', "-", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name.rstrip(". ") or "arquivo"


def check_tesseract(lang: str = "por") -> tuple[bool, str]:
    tesseract_path = shutil.which("tesseract")
    if not tesseract_path:
        return False, (
            "Tesseract não encontrado no ambiente. "
            "No Streamlit Cloud, adicione um arquivo packages.txt com:\n"
            "tesseract-ocr\n"
            "tesseract-ocr-por"
        )

    pytesseract.pytesseract.tesseract_cmd = tesseract_path

    try:
        langs = pytesseract.get_languages(config="")
    except Exception as exc:
        return False, f"Não foi possível listar idiomas do Tesseract: {exc}"

    if lang not in langs:
        return False, (
            f"O idioma '{lang}' não está instalado no Tesseract. "
            "No Streamlit Cloud, adicione em packages.txt:\n"
            "tesseract-ocr\n"
            "tesseract-ocr-por"
        )

    return True, tesseract_path


def iter_supported_files(root: Path) -> Iterable[Path]:
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        ext = path.suffix.lower()
        if ext in SUPPORTED_PDF_EXTS or ext in SUPPORTED_IMAGE_EXTS:
            yield path


def ocr_image(image: Image.Image, lang: str = "por", psm: int = 6) -> str:
    if image.mode != "RGB":
        image = image.convert("RGB")
    return pytesseract.image_to_string(image, lang=lang, config=f"--psm {psm}")


def ocr_image_file(path: Path, lang: str = "por", psm: int = 6) -> str:
    with Image.open(path) as img:
        return ocr_image(img, lang=lang, psm=psm).strip()


def ocr_pdf(path: Path, lang: str = "por", dpi: int = 300, psm: int = 6) -> Tuple[str, int]:
    text_parts: List[str] = []
    page_count = 0

    with fitz.open(path) as doc:
        for i, page in enumerate(doc, start=1):
            page_count += 1
            pix = page.get_pixmap(dpi=dpi, alpha=False)
            img = Image.open(io.BytesIO(pix.tobytes("png")))
            page_text = ocr_image(img, lang=lang, psm=psm).strip()
            text_parts.append(f"\n\n===== PÁGINA {i} =====\n\n{page_text}")

    return "".join(text_parts).strip(), page_count


def process_zip_bytes(
    zip_bytes: bytes,
    lang: str = "por",
    dpi: int = 300,
    psm: int = 6,
    progress_callback=None,
) -> tuple[bytes, List[dict]]:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        extracted_dir = tmp / "extraido"
        output_dir = tmp / "saida"
        extracted_dir.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)

        input_zip_path = tmp / "entrada.zip"
        input_zip_path.write_bytes(zip_bytes)

        with zipfile.ZipFile(input_zip_path, "r") as zf:
            zf.extractall(extracted_dir)

        files = list(iter_supported_files(extracted_dir))
        manifest_rows: List[dict] = []

        total = len(files)

        for idx, src in enumerate(files, start=1):
            rel = src.relative_to(extracted_dir)
            base_name = slugify_filename(str(rel.with_suffix("")).replace(os.sep, " - "))

            try:
                if src.suffix.lower() in SUPPORTED_PDF_EXTS:
                    text, pages = ocr_pdf(src, lang=lang, dpi=dpi, psm=psm)
                    out_name = f"{base_name}.txt"
                    out_path = output_dir / out_name
                    out_path.write_text(text, encoding="utf-8")

                    manifest_rows.append(
                        {
                            "arquivo_origem": str(rel),
                            "tipo": "pdf",
                            "unidades_processadas": pages,
                            "arquivo_saida": out_name,
                            "status": "ok",
                            "erro": "",
                        }
                    )
                else:
                    text = ocr_image_file(src, lang=lang, psm=psm)
                    out_name = f"{base_name}.txt"
                    out_path = output_dir / out_name
                    out_path.write_text(text, encoding="utf-8")

                    manifest_rows.append(
                        {
                            "arquivo_origem": str(rel),
                            "tipo": "imagem",
                            "unidades_processadas": 1,
                            "arquivo_saida": out_name,
                            "status": "ok",
                            "erro": "",
                        }
                    )
            except Exception as exc:
                err_name = f"{base_name}.erro.txt"
                err_path = output_dir / err_name
                err_path.write_text(str(exc), encoding="utf-8")

                manifest_rows.append(
                    {
                        "arquivo_origem": str(rel),
                        "tipo": src.suffix.lower().lstrip("."),
                        "unidades_processadas": 0,
                        "arquivo_saida": err_name,
                        "status": "erro",
                        "erro": str(exc),
                    }
                )

            if progress_callback:
                progress_callback(idx, total, str(rel))

        manifest_path = output_dir / "manifesto_ocr.csv"
        with manifest_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=[
                    "arquivo_origem",
                    "tipo",
                    "unidades_processadas",
                    "arquivo_saida",
                    "status",
                    "erro",
                ],
            )
            writer.writeheader()
            writer.writerows(manifest_rows)

        readme_path = output_dir / "README.txt"
        readme_path.write_text(
            "\n".join(
                [
                    "Resultado do OCR em português (Tesseract lang='por').",
                    "",
                    "Conteúdo deste ZIP:",
                    "- Um arquivo .txt para cada PDF/imagem processado",
                    "- manifesto_ocr.csv com o status de cada item",
                    "- arquivos .erro.txt quando algum item falhar",
                ]
            ),
            encoding="utf-8",
        )

        result_zip_path = tmp / "resultado_ocr.zip"
        with zipfile.ZipFile(result_zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for file in sorted(output_dir.rglob("*")):
                if file.is_file():
                    zf.write(file, arcname=file.relative_to(output_dir))

        return result_zip_path.read_bytes(), manifest_rows


st.set_page_config(page_title="OCR de ZIP em pt-BR", page_icon="🧾", layout="centered")

st.title("🧾 OCR de arquivos ZIP em português")
st.caption("Envie um .zip com PDFs e/ou imagens. O app faz OCR em pt-BR e devolve um novo .zip com os textos extraídos.")

ok, msg = check_tesseract("por")
if ok:
    st.success(f"Tesseract OK: {msg}")
else:
    st.error(msg)
    st.stop()

with st.expander("Configurações", expanded=True):
    dpi = st.slider("DPI para PDFs", min_value=150, max_value=400, value=300, step=50)
    psm = st.selectbox("Modo de segmentação (PSM)", options=[3, 4, 6, 11, 12], index=2)
    st.caption("PSM 6 costuma funcionar bem para páginas com blocos regulares de texto.")

uploaded = st.file_uploader("Envie o arquivo .zip", type=["zip"])

if "result_zip_bytes" not in st.session_state:
    st.session_state.result_zip_bytes = None
if "manifest_rows" not in st.session_state:
    st.session_state.manifest_rows = []

if uploaded is not None:
    if st.button("Processar OCR", type="primary", use_container_width=True):
        progress = st.progress(0, text="Iniciando...")
        status_box = st.empty()

        def update_progress(current: int, total: int, filename: str) -> None:
            fraction = current / total if total else 1.0
            progress.progress(fraction, text=f"Processando {current}/{total}")
            status_box.info(f"Arquivo atual: {filename}")

        try:
            result_zip_bytes, manifest_rows = process_zip_bytes(
                zip_bytes=uploaded.read(),
                lang="por",
                dpi=dpi,
                psm=psm,
                progress_callback=update_progress,
            )
            st.session_state.result_zip_bytes = result_zip_bytes
            st.session_state.manifest_rows = manifest_rows
            progress.progress(1.0, text="Concluído")
            status_box.success("OCR finalizado com sucesso.")
        except Exception as exc:
            progress.empty()
            status_box.error(f"Erro durante o OCR: {exc}")

if st.session_state.result_zip_bytes:
    st.download_button(
        label="📦 Baixar ZIP com OCR",
        data=st.session_state.result_zip_bytes,
        file_name="resultado_ocr.zip",
        mime="application/zip",
        use_container_width=True,
    )

    ok_count = sum(1 for row in st.session_state.manifest_rows if row["status"] == "ok")
    err_count = sum(1 for row in st.session_state.manifest_rows if row["status"] == "erro")

    st.write(f"Processados com sucesso: **{ok_count}**")
    st.write(f"Com erro: **{err_count}**")

    with st.expander("Manifesto de processamento", expanded=False):
        for row in st.session_state.manifest_rows:
            st.write(
                f"- {row['arquivo_origem']} | status={row['status']} | saída={row['arquivo_saida']}"
            )
