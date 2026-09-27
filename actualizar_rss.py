import hashlib
import html
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import urljoin
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup


# ============================================================
# CONFIGURACIÓN
# ============================================================

USUARIO_GITHUB = "plis2100"
REPOSITORIO = "cnmv-directivos-rss"

URL_RESULTADOS = (
    "https://api.cnmv.es/portal/consultas/"
    "directivos-resultado"
)

URL_CONSULTA = (
    "https://www.cnmv.es/portal/consultas/"
    "directivos-consulta?lang=es"
)

URL_RSS = (
    "https://raw.githubusercontent.com/"
    f"{USUARIO_GITHUB}/{REPOSITORIO}/main/feed.xml"
)

ARCHIVO_RSS = Path("feed.xml")
ARCHIVO_HISTORIAL = Path("historial.json")

# En la primera ejecución recuperará el último año.
DIAS_BUSQUEDA = 365

MAXIMO_PAGINAS = 25
MAXIMO_ENTRADAS = 1000

ZONA_HORARIA = ZoneInfo("Europe/Madrid")

CABECERAS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 "
        "Chrome/136.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,"
        "application/xml;q=0.9,*/*;q=0.8"
    ),
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.7",
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


# ============================================================
# DESCARGA DIRECTA DE LA CNMV
# ============================================================

def descargar_pagina(
    sesion,
    fecha_desde,
    fecha_hasta,
    pagina,
):
    parametros = {
        "fechaDesde": fecha_desde.strftime(
            "%d/%m/%Y"
        ),
        "fechaHasta": fecha_hasta.strftime(
            "%d/%m/%Y"
        ),
        "lang": "es",
        "page": pagina,
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
                f"Página {pagina}: "
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
                f"Intento {intento} fallido: "
                f"{error}"
            )

            if intento < 3:
                time.sleep(intento * 5)

    raise RuntimeError(
        f"No se pudo descargar la página "
        f"{pagina}: {ultimo_error}"
    )


def obtener_numero_paginas(sopa):
    numero_maximo = 1

    texto_pagina = limpiar(
        sopa.get_text(" ", strip=True)
    )

    coincidencias = re.findall(
        r"Página\s+\d+\s+de\s+(\d+)",
        texto_pagina,
        flags=re.IGNORECASE,
    )

    coincidencias += re.findall(
        r"Page\s+\d+\s+(?:of|out\s+of)\s+(\d+)",
        texto_pagina,
        flags=re.IGNORECASE,
    )

    for valor in coincidencias:
        try:
            numero_maximo = max(
                numero_maximo,
                int(valor),
            )
        except ValueError:
            pass

    for enlace in sopa.find_all(
        "a",
        href=True,
    ):
        texto = limpiar(
            enlace.get_text(" ", strip=True)
        )

        if texto.isdigit():
            numero_maximo = max(
                numero_maximo,
                int(texto),
            )

    return min(
        numero_maximo,
        MAXIMO_PAGINAS,
    )


def descargar_resultados():
    sesion = requests.Session()
    sesion.headers.update(CABECERAS)

    fecha_hasta = datetime.now(
        ZONA_HORARIA
    )

    fecha_desde = fecha_hasta - timedelta(
        days=DIAS_BUSQUEDA
    )

    print(
        "Consultando notificaciones desde "
        f"{fecha_desde:%d/%m/%Y} hasta "
        f"{fecha_hasta:%d/%m/%Y}"
    )

    paginas = []

    contenido, url = descargar_pagina(
        sesion,
        fecha_desde,
        fecha_hasta,
        0,
    )

    paginas.append(
        {
            "contenido": contenido,
            "url": url,
        }
    )

    sopa = BeautifulSoup(
        contenido,
        "html.parser",
    )

    numero_paginas = obtener_numero_paginas(
        sopa
    )

    print(
        f"Número de páginas detectado: "
        f"{numero_paginas}"
    )

    # La primera página ya se descargó con page=0.
    for pagina in range(
        1,
        numero_paginas,
    ):
        contenido, url = descargar_pagina(
            sesion,
            fecha_desde,
            fecha_hasta,
            pagina,
        )

        paginas.append(
            {
                "contenido": contenido,
                "url": url,
            }
        )

    return paginas


# ============================================================
# EXTRACCIÓN DE NOTIFICACIONES
# ============================================================

def encontrar_bloques(sopa):
    patron = re.compile(
        r"(?:Número|N[uú]mero|Register)"
        r"\s+(?:de\s+)?(?:registro|number)"
        r"\s*:\s*([0-9]+)",
        re.IGNORECASE,
    )

    bloques = []
    vistos = set()

    for elemento in sopa.find_all(
        ["li", "article", "tr", "div"]
    ):
        texto = limpiar(
            elemento.get_text(
                " ",
                strip=True,
            )
        )

        registros = patron.findall(texto)

        # El bloque correcto debe contener
        # exactamente una notificación.
        if len(registros) != 1:
            continue

        registro = registros[0]

        if registro in vistos:
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
    patrones = [
        (
            r"Declarante\s*:\s*(.+?)"
            r"(?=Motivo\s+de\s+la\s+notificación"
            r"|Número\s+de\s+registro|$)"
        ),
        (
            r"Declarant\s*:\s*(.+?)"
            r"(?=Reason\s+for\s+notification"
            r"|Register\s+number|$)"
        ),
    ]

    for patron in patrones:
        coincidencia = re.search(
            patron,
            texto,
            flags=re.IGNORECASE,
        )

        if coincidencia:
            return limpiar(
                coincidencia.group(1)
            )

    return "Declarante no identificado"


def obtener_motivo(texto):
    patrones = [
        (
            r"Motivo\s+de\s+la\s+notificación"
            r"\s*:\s*(.+?)"
            r"(?=Número\s+de\s+registro|$)"
        ),
        (
            r"Reason\s+for\s+notification"
            r"\s*:\s*(.+?)"
            r"(?=Register\s+number|$)"
        ),
    ]

    for patron in patrones:
        coincidencia = re.search(
            patron,
            texto,
            flags=re.IGNORECASE,
        )

        if coincidencia:
            return limpiar(
                coincidencia.group(1)
            )

    return ""


def obtener_empresa(
    texto,
    fecha_texto,
):
    resultado = texto

    if fecha_texto:
        resultado = resultado.replace(
            fecha_texto,
            "",
            1,
        )

    resultado = re.split(
        r"Declarante\s*:|Declarant\s*:",
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


def obtener_enlace(
    elemento,
    url_base,
    registro,
):
    enlaces = elemento.find_all(
        "a",
        href=True,
    )

    for enlace in enlaces:
        href = limpiar(
            enlace.get("href")
        )

        if not href:
            continue

        if href.startswith(
            ("javascript:", "#")
        ):
            continue

        return urljoin(
            url_base,
            href,
        )

    # Si no hay enlace directo, se mantiene
    # la página de resultados.
    return url_base


def extraer_notificaciones_pagina(
    contenido,
    url_base,
):
    sopa = BeautifulSoup(
        contenido,
        "html.parser",
    )

    notificaciones = []

    for bloque in encontrar_bloques(sopa):
        elemento = bloque["elemento"]
        texto = bloque["texto"]
        registro = bloque["registro"]

        coincidencia_fecha = re.search(
            r"\b\d{2}/\d{2}/\d{4}\b",
            texto,
        )

        fecha_texto = (
            coincidencia_fecha.group(0)
            if coincidencia_fecha
            else ""
        )

        fecha = convertir_fecha(
            fecha_texto
        )

        declarante = obtener_declarante(
            texto
        )

        motivo = obtener_motivo(texto)

        empresa = obtener_empresa(
            texto,
            fecha_texto,
        )

        enlace = obtener_enlace(
            elemento,
            url_base,
            registro,
        )

        titulo = (
            "CNMV DIRECTIVOS | "
            f"{fecha_texto or 'SIN FECHA'} | "
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
                "Abrir en la CNMV"
                "</a></p>"
            )
        )

        identificador = hashlib.sha256(
            (
                "cnmv-directivos-v2|"
                f"{registro}"
            ).encode("utf-8")
        ).hexdigest()

        notificaciones.append(
            {
                "id": identificador,
                "registro": registro,
                "titulo": titulo,
                "url": enlace,
                "descripcion": "".join(
                    descripcion
                ),
                "fecha": (
                    fecha.isoformat()
                    if fecha
                    else datetime.now(
                        timezone.utc
                    ).isoformat()
                ),
            }
        )

    return notificaciones


def extraer_todas(paginas):
    resultado = []
    registros_vistos = set()

    for numero, pagina in enumerate(
        paginas,
        start=1,
    ):
        encontradas = (
            extraer_notificaciones_pagina(
                pagina["contenido"],
                pagina["url"],
            )
        )

        print(
            f"Página {numero}: "
            f"{len(encontradas)} notificaciones"
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
# HISTORIAL
# ============================================================

def cargar_historial():
    if not ARCHIVO_HISTORIAL.exists():
        return []

    try:
        datos = json.loads(
            ARCHIVO_HISTORIAL.read_text(
                encoding="utf-8"
            )
        )

        if isinstance(datos, list):
            return datos

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
        "Notificaciones de directivos y "
        "personas vinculadas publicadas "
        "por la CNMV."
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
    ET.indent(arbol, space="  ")

    arbol.write(
        ARCHIVO_RSS,
        encoding="utf-8",
        xml_declaration=True,
    )

    # Verifica que el XML creado sea válido.
    ET.parse(ARCHIVO_RSS)

    print(
        f"feed.xml generado: "
        f"{ARCHIVO_RSS.stat().st_size} bytes"
    )


# ============================================================
# PROGRAMA PRINCIPAL
# ============================================================

def main():
    print("========================================")
    print("NOTIFICACIONES DE DIRECTIVOS CNMV")
    print("========================================")

    paginas = descargar_resultados()

    nuevas = extraer_todas(paginas)

    anteriores = cargar_historial()

    resultado = mezclar_notificaciones(
        nuevas,
        anteriores,
    )

    guardar_historial(resultado)
    crear_rss(resultado)

    print("")
    print("Proceso finalizado correctamente.")
    print(
        f"Notificaciones encontradas: "
        f"{len(nuevas)}"
    )
    print(
        f"Entradas guardadas en RSS: "
        f"{len(resultado)}"
    )
    print(f"URL para Feedly: {URL_RSS}")

    if not nuevas:
        print(
            "AVISO: la CNMV respondió, pero "
            "no se extrajo ninguna notificación."
        )


if __name__ == "__main__":
    try:
        main()

    except Exception as error:
        print(
            f"ERROR: {type(error).__name__}: "
            f"{error}",
            file=sys.stderr,
        )
        sys.exit(1)
