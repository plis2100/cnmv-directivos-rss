import hashlib
import html
import json
import re
import sys
import time
from decimal import Decimal, InvalidOperation
from io import BytesIO
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import urljoin
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader


# ============================================================
# CONFIGURACIÓN
# ============================================================

USUARIO_GITHUB = "plis2100"
REPOSITORIO = "cnmv-directivos-rss"

URL_RESULTADOS = (
    "https://www.cnmv.es/portal/Consultas/"
    "Directivos-Resultado"
)

URL_CONSULTA = (
    "https://www.cnmv.es/portal/Consultas/"
    "Directivos-Consulta"
)

URL_RSS = (
    "https://raw.githubusercontent.com/"
    f"{USUARIO_GITHUB}/{REPOSITORIO}/main/feed.xml"
)

ARCHIVO_RSS = Path("feed.xml")
ARCHIVO_HISTORIAL = Path("historial.json")

DIAS_PRIMERA_EJECUCION = 60
DIAS_EJECUCIONES_POSTERIORES = 7
MAXIMO_ENTRADAS = 1000

ZONA_HORARIA = ZoneInfo("Europe/Madrid")

CABECERAS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/136.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,"
        "application/xml;q=0.9,*/*;q=0.8"
    ),
    "Accept-Language": "es-ES,es;q=0.9",
    "Referer": URL_CONSULTA,
}


# ============================================================
# FUNCIONES AUXILIARES
# ============================================================

def limpiar(valor):
    if valor is None:
        return ""

    return " ".join(str(valor).split()).strip()


def convertir_fecha(valor):
    texto = limpiar(valor)

    if not texto:
        return None

    coincidencia = re.search(
        r"\b(\d{2})/(\d{2})/(\d{4})\b",
        texto,
    )

    if coincidencia:
        dia, mes, anio = coincidencia.groups()

        return datetime(
            int(anio),
            int(mes),
            int(dia),
            12,
            0,
            tzinfo=ZONA_HORARIA,
        )

    try:
        fecha = datetime.fromisoformat(
            texto.replace("Z", "+00:00")
        )

        if fecha.tzinfo is None:
            fecha = fecha.replace(
                tzinfo=timezone.utc
            )

        return fecha.astimezone(
            ZONA_HORARIA
        )

    except ValueError:
        return None


def fecha_rss(fecha):
    if fecha is None:
        fecha = datetime.now(timezone.utc)

    if fecha.tzinfo is None:
        fecha = fecha.replace(
            tzinfo=timezone.utc
        )

    return format_datetime(
        fecha.astimezone(timezone.utc)
    )


def numero_decimal_es(valor):
    texto = limpiar(valor).replace(" ", "")

    if not texto:
        return None

    if "," in texto:
        texto = texto.replace(".", "")
        texto = texto.replace(",", ".")

    elif texto.count(".") > 1:
        texto = texto.replace(".", "")

    try:
        return Decimal(texto)

    except InvalidOperation:
        return None


def formatear_numero(valor, decimales=2):
    if valor is None:
        return "No publicado"

    cuantizador = Decimal("1").scaleb(-decimales)
    texto = f"{valor.quantize(cuantizador):,.{decimales}f}"

    return (
        texto
        .replace(",", "X")
        .replace(".", ",")
        .replace("X", ".")
    )


def formatear_acciones(valor):
    if valor is None:
        return "No publicado"

    if valor == valor.to_integral_value():
        texto = f"{int(valor):,}"
        return texto.replace(",", ".")

    return formatear_numero(valor, 2)


# ============================================================
# DESCARGA DE LA CNMV
# ============================================================

def descargar_dia(sesion, fecha):
    fecha_texto = fecha.strftime("%d/%m/%Y")

    parametros = {
        "fechad": fecha_texto,
        "lang": "es",
    }

    ultimo_error = None

    for intento in range(1, 4):
        try:
            respuesta = sesion.get(
                URL_RESULTADOS,
                params=parametros,
                timeout=(15, 60),
            )

            print(
                f"{fecha_texto}: "
                f"HTTP {respuesta.status_code}"
            )

            respuesta.raise_for_status()

            if not respuesta.text.strip():
                raise RuntimeError(
                    "La CNMV devolvió una página vacía."
                )

            return respuesta.text, respuesta.url

        except (
            requests.RequestException,
            RuntimeError,
        ) as error:
            ultimo_error = error

            print(
                f"Intento {intento} fallido "
                f"para {fecha_texto}: {error}"
            )

            if intento < 3:
                time.sleep(intento * 3)

    print(
        f"No se pudo descargar {fecha_texto}: "
        f"{ultimo_error}"
    )

    return "", ""


def descargar_resultados():
    sesion = requests.Session()
    sesion.headers.update(CABECERAS)

    primera_ejecucion = (
        not ARCHIVO_HISTORIAL.exists()
    )

    if primera_ejecucion:
        dias = DIAS_PRIMERA_EJECUCION
    else:
        dias = DIAS_EJECUCIONES_POSTERIORES

    hoy = datetime.now(ZONA_HORARIA)

    print(
        f"Consultando los últimos {dias} días."
    )

    paginas = []

    for desplazamiento in range(dias):
        fecha = hoy - timedelta(
            days=desplazamiento
        )

        contenido, url = descargar_dia(
            sesion,
            fecha,
        )

        if contenido:
            paginas.append(
                {
                    "fecha": fecha,
                    "contenido": contenido,
                    "url": url,
                }
            )

        time.sleep(0.25)

    return paginas


# ============================================================
# EXTRACCIÓN DEL LISTADO
# ============================================================

def patron_registro():
    return re.compile(
        r"(?:Número|N[uú]mero)\s+de\s+registro"
        r"\s*:\s*([0-9]+)",
        re.IGNORECASE,
    )


def encontrar_bloques(sopa):
    patron = patron_registro()
    bloques = []
    vistos = set()

    for elemento in sopa.find_all(
        ["li", "article", "section", "tr", "div"]
    ):
        texto = limpiar(
            elemento.get_text(
                " ",
                strip=True,
            )
        )

        registros = patron.findall(texto)

        if len(registros) != 1:
            continue

        registro = registros[0]

        if registro in vistos:
            continue

        if not re.search(
            r"Declarante\s*:",
            texto,
            flags=re.IGNORECASE,
        ):
            continue

        vistos.add(registro)

        bloques.append(
            {
                "elemento": elemento,
                "texto": texto,
                "registro": registro,
            }
        )

    return bloques


def obtener_declarante(texto):
    coincidencia = re.search(
        r"Declarante\s*:\s*(.+?)"
        r"(?=Motivo\s+de\s+la\s+notificación"
        r"|Número\s+de\s+registro|$)",
        texto,
        flags=re.IGNORECASE,
    )

    if coincidencia:
        return limpiar(
            coincidencia.group(1)
        )

    return "Declarante no identificado"


def obtener_motivo(texto):
    coincidencia = re.search(
        r"Motivo\s+de\s+la\s+notificación"
        r"\s*:\s*(.+?)"
        r"(?=Número\s+de\s+registro|$)",
        texto,
        flags=re.IGNORECASE,
    )

    if coincidencia:
        return limpiar(
            coincidencia.group(1)
        )

    return ""


def obtener_empresa(texto, fecha_texto):
    resultado = texto

    if fecha_texto:
        resultado = resultado.replace(
            fecha_texto,
            "",
            1,
        )

    resultado = re.split(
        r"Declarante\s*:",
        resultado,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]

    resultado = re.sub(
        r"^[•\-–—\s;]+",
        "",
        resultado,
    )

    resultado = limpiar(resultado)

    return (
        resultado
        or "Empresa no identificada"
    )


def obtener_enlaces(elemento, url_base):
    enlace_empresa = url_base
    enlace_documento = ""

    for enlace in elemento.find_all(
        "a",
        href=True,
    ):
        href = limpiar(
            enlace.get("href")
        )

        if not href:
            continue

        if href.startswith(
            ("javascript:", "#")
        ):
            continue

        url = urljoin(
            url_base,
            href,
        )

        if (
            "webservices/verdocumento"
            in url.lower()
        ):
            enlace_documento = url

        elif enlace_empresa == url_base:
            enlace_empresa = url

    return enlace_empresa, enlace_documento


def crear_notificacion(
    elemento,
    texto,
    registro,
    url_base,
    fecha_consultada,
):
    coincidencia_fecha = re.search(
        r"\b\d{2}/\d{2}/\d{4}\b",
        texto,
    )

    if coincidencia_fecha:
        fecha_texto = (
            coincidencia_fecha.group(0)
        )

        fecha = convertir_fecha(
            fecha_texto
        )

    else:
        fecha = fecha_consultada

        fecha_texto = fecha.strftime(
            "%d/%m/%Y"
        )

    declarante = obtener_declarante(texto)
    motivo = obtener_motivo(texto)

    empresa = obtener_empresa(
        texto,
        fecha_texto,
    )

    (
        enlace_empresa,
        enlace_documento,
    ) = obtener_enlaces(
        elemento,
        url_base,
    )

    enlace = (
        enlace_documento
        or enlace_empresa
    )

    titulo = (
        "CNMV DIRECTIVOS | "
        f"{fecha_texto} | "
        f"{empresa} | "
        f"{declarante}"
    )

    descripcion = [
        (
            "<p><strong>Empresa:</strong> "
            f"{html.escape(empresa)}</p>"
        ),
        (
            "<p><strong>Declarante:</strong> "
            f"{html.escape(declarante)}</p>"
        ),
        (
            "<p><strong>Fecha:</strong> "
            f"{html.escape(fecha_texto)}</p>"
        ),
        (
            "<p><strong>Número de registro:"
            "</strong> "
            f"{html.escape(registro)}</p>"
        ),
    ]

    if motivo:
        descripcion.append(
            (
                "<p><strong>Motivo:</strong> "
                f"{html.escape(motivo)}</p>"
            )
        )

    descripcion.append(
        (
            f'<p><a href="{html.escape(enlace)}">'
            "Abrir notificación en la CNMV"
            "</a></p>"
        )
    )

    identificador = hashlib.sha256(
        (
            "cnmv-directivos-operaciones-v2|"
            f"{registro}"
        ).encode("utf-8")
    ).hexdigest()

    return {
        "id": identificador,
        "registro": registro,
        "titulo": titulo,
        "url": enlace,
        "url_empresa": enlace_empresa,
        "url_documento": enlace_documento,
        "descripcion": "".join(
            descripcion
        ),
        "fecha": fecha.isoformat(),
        "empresa": empresa,
        "declarante": declarante,
        "fecha_texto": fecha_texto,
        "motivo": motivo,
    }


def extraer_pagina(pagina):
    sopa = BeautifulSoup(
        pagina["contenido"],
        "html.parser",
    )

    notificaciones = []

    bloques = encontrar_bloques(sopa)

    for bloque in bloques:
        notificacion = crear_notificacion(
            bloque["elemento"],
            bloque["texto"],
            bloque["registro"],
            pagina["url"],
            pagina["fecha"],
        )

        notificaciones.append(
            notificacion
        )

    return notificaciones


def extraer_todas(paginas):
    resultado = []
    registros_vistos = set()

    for pagina in paginas:
        encontradas = extraer_pagina(
            pagina
        )

        fecha_texto = pagina[
            "fecha"
        ].strftime("%d/%m/%Y")

        print(
            f"{fecha_texto}: "
            f"{len(encontradas)} "
            "notificaciones encontradas"
        )

        for notificacion in encontradas:
            registro = notificacion[
                "registro"
            ]

            if registro in registros_vistos:
                continue

            registros_vistos.add(registro)
            resultado.append(notificacion)

    return resultado


# ============================================================
# LECTURA DE LOS PDF
# ============================================================

def extraer_texto_pdf(contenido):
    lector = PdfReader(
        BytesIO(contenido)
    )

    partes = []

    for pagina in lector.pages:
        try:
            texto = pagina.extract_text(
                extraction_mode="layout"
            )

        except TypeError:
            texto = pagina.extract_text()

        if texto:
            partes.append(texto)

    return "\n".join(partes)


def extraer_operaciones(texto):
    operaciones = []

    naturalezas = (
        "Compra|Venta|Adquisici[oó]n|"
        "Transmisi[oó]n|Suscripci[oó]n|"
        "Canje|Donaci[oó]n|Ejercicio|"
        "Aceptaci[oó]n|Entrega|Recepci[oó]n"
    )

    patron = re.compile(
        rf"\b(?P<tipo>{naturalezas})\b\s+"
        r"(?P<fecha>\d{2}/\d{2}/\d{4})\s+"
        r"(?P<lugar>[A-Z0-9.\- ]{2,30}?)\s+"
        r"(?P<volumen>\d[\d.,]*)\s+"
        r"(?P<precio>\d[\d.,]*)\s+"
        r"(?P<divisa>[A-Z]{3})\b",
        flags=re.IGNORECASE,
    )

    for linea in texto.splitlines():
        linea_limpia = limpiar(linea)

        coincidencia = patron.search(
            linea_limpia
        )

        if not coincidencia:
            continue

        volumen = numero_decimal_es(
            coincidencia.group("volumen")
        )

        precio = numero_decimal_es(
            coincidencia.group("precio")
        )

        operaciones.append(
            {
                "tipo": limpiar(
                    coincidencia.group("tipo")
                ).capitalize(),
                "fecha": coincidencia.group(
                    "fecha"
                ),
                "lugar": limpiar(
                    coincidencia.group("lugar")
                ),
                "volumen": volumen,
                "precio": precio,
                "divisa": coincidencia.group(
                    "divisa"
                ).upper(),
            }
        )

    return operaciones


def reconstruir_presentacion(
    notificacion,
    operaciones,
):
    empresa = notificacion.get(
        "empresa",
        "Empresa no identificada",
    )

    declarante = notificacion.get(
        "declarante",
        "Declarante no identificado",
    )

    fecha_texto = notificacion.get(
        "fecha_texto",
        "",
    )

    registro = notificacion.get(
        "registro",
        "",
    )

    motivo = notificacion.get(
        "motivo",
        "",
    )

    documento = notificacion.get(
        "url_documento",
        "",
    )

    if operaciones:
        principal = operaciones[0]

        titulo = (
            "CNMV DIRECTIVOS | "
            f"{empresa} | "
            f"{declarante} | "
            f"{principal['tipo'].upper()}: "
            f"{formatear_acciones(principal['volumen'])} "
            "acciones | "
            f"{formatear_numero(principal['precio'], 4)} "
            f"{principal['divisa']} | "
            f"{principal['fecha']} | "
            f"{principal['lugar']}"
        )

        if len(operaciones) > 1:
            titulo += (
                f" | +{len(operaciones) - 1} "
                "operaciones"
            )

    else:
        titulo = (
            "CNMV DIRECTIVOS | "
            f"{fecha_texto} | "
            f"{empresa} | "
            f"{declarante}"
        )

    descripcion = [
        (
            "<p><strong>Empresa:</strong> "
            f"{html.escape(empresa)}</p>"
        ),
        (
            "<p><strong>Declarante:</strong> "
            f"{html.escape(declarante)}</p>"
        ),
        (
            "<p><strong>Fecha de publicación:"
            "</strong> "
            f"{html.escape(fecha_texto)}</p>"
        ),
        (
            "<p><strong>Número de registro:"
            "</strong> "
            f"{html.escape(registro)}</p>"
        ),
    ]

    if motivo:
        descripcion.append(
            (
                "<p><strong>Motivo:</strong> "
                f"{html.escape(motivo)}</p>"
            )
        )

    descripcion.append("<hr>")

    if operaciones:
        descripcion.append(
            "<p><strong>"
            "OPERACIONES DECLARADAS"
            "</strong></p><ul>"
        )

        for operacion in operaciones:
            descripcion.append(
                "<li>"
                f"<strong>{html.escape(operacion['tipo'])}:"
                "</strong> "
                f"{formatear_acciones(operacion['volumen'])} "
                "acciones; "
                f"<strong>precio:</strong> "
                f"{formatear_numero(operacion['precio'], 4)} "
                f"{html.escape(operacion['divisa'])}; "
                f"<strong>fecha:</strong> "
                f"{html.escape(operacion['fecha'])}; "
                f"<strong>mercado:</strong> "
                f"{html.escape(operacion['lugar'])}."
                "</li>"
            )

        descripcion.append("</ul>")

    else:
        descripcion.append(
            "<p><strong>Operaciones:</strong> "
            "no se pudieron extraer automáticamente "
            "del documento.</p>"
        )

    if documento:
        descripcion.append(
            f'<p><a href="{html.escape(documento)}">'
            "Abrir documento oficial de la CNMV"
            "</a></p>"
        )

    notificacion["titulo"] = titulo

    notificacion["descripcion"] = "".join(
        descripcion
    )

    notificacion["detalle_extraido"] = True

    return notificacion


def completar_detalles(notificaciones):
    sesion = requests.Session()
    sesion.headers.update(CABECERAS)

    total = len(notificaciones)

    for indice, notificacion in enumerate(
        notificaciones,
        start=1,
    ):
        documento = notificacion.get(
            "url_documento",
            "",
        )

        if not documento:
            print(
                f"Registro "
                f"{notificacion.get('registro')}: "
                "sin enlace al documento."
            )

            continue

        try:
            respuesta = sesion.get(
                documento,
                timeout=(15, 60),
            )

            respuesta.raise_for_status()

            if not respuesta.content.startswith(
                b"%PDF"
            ):
                raise RuntimeError(
                    "El documento descargado "
                    "no es un PDF."
                )

            texto = extraer_texto_pdf(
                respuesta.content
            )

            operaciones = extraer_operaciones(
                texto
            )

            reconstruir_presentacion(
                notificacion,
                operaciones,
            )

            print(
                f"Detalle {indice}/{total}: "
                f"registro "
                f"{notificacion.get('registro')} - "
                f"{len(operaciones)} operaciones."
            )

        except Exception as error:
            print(
                f"Registro "
                f"{notificacion.get('registro')}: "
                "no se pudo analizar el PDF "
                f"({error})."
            )

        time.sleep(0.20)

    return notificaciones


# ============================================================
# HISTORIAL
# ============================================================

def cargar_historial():
    if not ARCHIVO_HISTORIAL.exists():
        return []

    try:
        contenido = json.loads(
            ARCHIVO_HISTORIAL.read_text(
                encoding="utf-8"
            )
        )

        if isinstance(contenido, list):
            return contenido

    except (
        OSError,
        json.JSONDecodeError,
    ):
        pass

    return []


def guardar_historial(notificaciones):
    ARCHIVO_HISTORIAL.write_text(
        json.dumps(
            notificaciones[
                :MAXIMO_ENTRADAS
            ],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def mezclar_notificaciones(
    nuevas,
    anteriores,
):
    por_registro = {}

    for notificacion in anteriores:
        registro = notificacion.get(
            "registro"
        )

        if registro:
            por_registro[registro] = (
                notificacion
            )

    for notificacion in nuevas:
        por_registro[
            notificacion["registro"]
        ] = notificacion

    resultado = list(
        por_registro.values()
    )

    resultado.sort(
        key=lambda elemento: elemento.get(
            "fecha",
            "",
        ),
        reverse=True,
    )

    return resultado[:MAXIMO_ENTRADAS]


# ============================================================
# CREACIÓN DEL RSS
# ============================================================

def crear_rss(notificaciones):
    ET.register_namespace(
        "atom",
        "http://www.w3.org/2005/Atom",
    )

    rss = ET.Element(
        "rss",
        {"version": "2.0"},
    )

    canal = ET.SubElement(
        rss,
        "channel",
    )

    ET.SubElement(
        canal,
        "title",
    ).text = (
        "Notificaciones de directivos CNMV"
    )

    ET.SubElement(
        canal,
        "link",
    ).text = URL_CONSULTA

    ET.SubElement(
        canal,
        "description",
    ).text = (
        "Operaciones de directivos y personas "
        "vinculadas publicadas por la CNMV."
    )

    ET.SubElement(
        canal,
        "language",
    ).text = "es-ES"

    ET.SubElement(
        canal,
        "lastBuildDate",
    ).text = fecha_rss(
        datetime.now(timezone.utc)
    )

    ET.SubElement(
        canal,
        "ttl",
    ).text = "120"

    ET.SubElement(
        canal,
        "{http://www.w3.org/2005/Atom}link",
        {
            "href": URL_RSS,
            "rel": "self",
            "type": "application/rss+xml",
        },
    )

    for notificacion in notificaciones:
        item = ET.SubElement(
            canal,
            "item",
        )

        ET.SubElement(
            item,
            "title",
        ).text = notificacion["titulo"]

        ET.SubElement(
            item,
            "link",
        ).text = notificacion["url"]

        ET.SubElement(
            item,
            "guid",
            {"isPermaLink": "false"},
        ).text = notificacion["id"]

        fecha = convertir_fecha(
            notificacion.get("fecha")
        )

        ET.SubElement(
            item,
            "pubDate",
        ).text = fecha_rss(fecha)

        ET.SubElement(
            item,
            "description",
        ).text = notificacion[
            "descripcion"
        ]

        ET.SubElement(
            item,
            "category",
        ).text = "CNMV DIRECTIVOS"

    arbol = ET.ElementTree(rss)

    ET.indent(
        arbol,
        space="  ",
    )

    arbol.write(
        ARCHIVO_RSS,
        encoding="utf-8",
        xml_declaration=True,
    )

    ET.parse(ARCHIVO_RSS)

    print(
        f"feed.xml generado: "
        f"{ARCHIVO_RSS.stat().st_size} bytes"
    )


# ============================================================
# PROGRAMA PRINCIPAL
# ============================================================

def main():
    print(
        "========================================"
    )

    print(
        "NOTIFICACIONES DE DIRECTIVOS CNMV"
    )

    print(
        "========================================"
    )

    paginas = descargar_resultados()

    nuevas = extraer_todas(
        paginas
    )

    nuevas = completar_detalles(
        nuevas
    )

    anteriores = cargar_historial()

    resultado = mezclar_notificaciones(
        nuevas,
        anteriores,
    )

    guardar_historial(
        resultado
    )

    crear_rss(
        resultado
    )

    print("")
    print(
        "Proceso finalizado correctamente."
    )

    print(
        "Notificaciones encontradas: "
        f"{len(nuevas)}"
    )

    print(
        "Entradas guardadas en RSS: "
        f"{len(resultado)}"
    )

    print(
        f"URL para Feedly: {URL_RSS}"
    )

    if not nuevas:
        print(
            "AVISO: no se extrajo ninguna "
            "notificación."
        )


if __name__ == "__main__":
    try:
        main()

    except Exception as error:
        print(
            f"ERROR: "
            f"{type(error).__name__}: "
            f"{error}",
            file=sys.stderr,
        )

        sys.exit(1)
