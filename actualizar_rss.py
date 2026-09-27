import hashlib
import html
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import urljoin
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup


USUARIO_GITHUB = "plis2100"
REPOSITORIO = "cnmv-directivos-rss"

URL_CNMV = (
    "https://www.cnmv.es/portal/consultas/"
    "directivos-consulta?lang=es"
)

URL_RSS = (
    "https://raw.githubusercontent.com/"
    f"{USUARIO_GITHUB}/{REPOSITORIO}/main/feed.xml"
)

ARCHIVO_RSS = Path("feed.xml")
ARCHIVO_HISTORIAL = Path("historial.json")

DIAS_BUSQUEDA = 15
MAXIMO_ENTRADAS = 500

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
}


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


def localizar_formulario(sopa):
    formularios = sopa.find_all("form")

    for formulario in formularios:
        texto = limpiar(
            formulario.get_text(
                " ",
                strip=True,
            )
        ).lower()

        if (
            "fecha" in texto
            or "notificación" in texto
            or "declarante" in texto
        ):
            return formulario

    if formularios:
        return formularios[0]

    return None


def crear_datos_formulario(
    formulario,
    fecha_desde,
    fecha_hasta,
):
    datos = {}

    for campo in formulario.find_all(
        ["input", "select", "textarea"]
    ):
        nombre = campo.get("name")

        if not nombre:
            continue

        tipo = limpiar(
            campo.get("type", "")
        ).lower()

        if tipo in {
            "submit",
            "button",
            "image",
            "file",
            "reset",
        }:
            continue

        datos[nombre] = campo.get(
            "value",
            "",
        )

    desde = fecha_desde.strftime(
        "%d/%m/%Y"
    )

    hasta = fecha_hasta.strftime(
        "%d/%m/%Y"
    )

    tiene_desde = False
    tiene_hasta = False

    for campo in formulario.find_all(
        ["input", "textarea"]
    ):
        nombre = campo.get("name")

        if not nombre:
            continue

        pista = (
            f"{nombre} "
            f"{campo.get('id', '')} "
            f"{campo.get('placeholder', '')}"
        ).lower()

        if any(
            palabra in pista
            for palabra in (
                "desde",
                "inicio",
                "fechaini",
                "fecha_ini",
                "datefrom",
                "fromdate",
            )
        ):
            datos[nombre] = desde
            tiene_desde = True

        elif any(
            palabra in pista
            for palabra in (
                "hasta",
                "fin",
                "fechafin",
                "fecha_fin",
                "dateto",
                "todate",
            )
        ):
            datos[nombre] = hasta
            tiene_hasta = True

    if not tiene_desde:
        datos["fechaDesde"] = desde

    if not tiene_hasta:
        datos["fechaHasta"] = hasta

    boton = formulario.find(
        ["button", "input"],
        attrs={"type": "submit"},
    )

    if boton and boton.get("name"):
        datos[boton["name"]] = (
            boton.get("value")
            or limpiar(boton.get_text())
            or "Buscar"
        )

    return datos


def consultar_cnmv():
    sesion = requests.Session()
    sesion.headers.update(CABECERAS)

    respuesta = sesion.get(
        URL_CNMV,
        timeout=(15, 60),
    )

    respuesta.raise_for_status()

    sopa = BeautifulSoup(
        respuesta.text,
        "html.parser",
    )

    formulario = localizar_formulario(sopa)

    if formulario is None:
        raise RuntimeError(
            "No se encontró el formulario de la CNMV."
        )

    ahora = datetime.now(ZONA_HORARIA)

    fecha_desde = ahora - timedelta(
        days=DIAS_BUSQUEDA
    )

    datos = crear_datos_formulario(
        formulario,
        fecha_desde,
        ahora,
    )

    accion = formulario.get("action")

    if accion:
        url_consulta = urljoin(
            respuesta.url,
            accion,
        )
    else:
        url_consulta = respuesta.url

    metodo = limpiar(
        formulario.get("method", "get")
    ).lower()

    print(
        f"Consultando desde "
        f"{fecha_desde:%d/%m/%Y} hasta "
        f"{ahora:%d/%m/%Y}"
    )

    if metodo == "post":
        resultado = sesion.post(
            url_consulta,
            data=datos,
            timeout=(15, 60),
        )
    else:
        resultado = sesion.get(
            url_consulta,
            params=datos,
            timeout=(15, 60),
        )

    resultado.raise_for_status()

    return resultado.text, resultado.url


def encontrar_bloques(sopa):
    patron_registro = re.compile(
        r"Número\s+de\s+registro\s*:\s*"
        r"([0-9]+)",
        re.IGNORECASE,
    )

    bloques = []
    vistos = set()

    for elemento in sopa.find_all(
        ["li", "article", "div", "tr"]
    ):
        texto = limpiar(
            elemento.get_text(
                " ",
                strip=True,
            )
        )

        registros = patron_registro.findall(
            texto
        )

        # Seleccionamos únicamente elementos que
        # contienen una sola notificación.
        if len(registros) != 1:
            continue

        numero = registros[0]

        if numero in vistos:
            continue

        vistos.add(numero)

        bloques.append(
            {
                "elemento": elemento,
                "texto": texto,
                "registro": numero,
            }
        )

    return bloques


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
        r"^[•\-–—\s]+",
        "",
        resultado,
    )

    resultado = limpiar(resultado)

    return (
        resultado
        or "Empresa no identificada"
    )


def extraer_notificaciones(
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

        fecha = convertir_fecha(fecha_texto)

        coincidencia_declarante = re.search(
            r"Declarante\s*:\s*(.+?)"
            r"(?=Motivo\s+de\s+la\s+notificación"
            r"|Número\s+de\s+registro|$)",
            texto,
            re.IGNORECASE,
        )

        if coincidencia_declarante:
            declarante = limpiar(
                coincidencia_declarante.group(1)
            )
        else:
            declarante = (
                "Declarante no identificado"
            )

        coincidencia_motivo = re.search(
            r"Motivo\s+de\s+la\s+notificación"
            r"\s*:\s*(.+?)"
            r"(?=Número\s+de\s+registro|$)",
            texto,
            re.IGNORECASE,
        )

        motivo = (
            limpiar(
                coincidencia_motivo.group(1)
            )
            if coincidencia_motivo
            else ""
        )

        empresa = obtener_empresa(
            texto,
            fecha_texto,
        )

        enlace = ""

        for etiqueta_enlace in elemento.find_all(
            "a",
            href=True,
        ):
            href = etiqueta_enlace.get("href", "")

            if not href:
                continue

            if href.startswith(
                ("javascript:", "#")
            ):
                continue

            enlace = urljoin(
                url_base,
                href,
            )

            break

        if not enlace:
            enlace = url_base

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
                "Abrir notificación en la CNMV"
                "</a></p>"
            )
        )

        identificador = hashlib.sha256(
            (
                "cnmv-directivos-v1|"
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
            notificaciones[:MAXIMO_ENTRADAS],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def mezclar_notificaciones(
    nuevas,
    anteriores,
):
    por_id = {}

    for notificacion in anteriores:
        identificador = notificacion.get("id")

        if identificador:
            por_id[identificador] = notificacion

    for notificacion in nuevas:
        por_id[
            notificacion["id"]
        ] = notificacion

    resultado = list(por_id.values())

    resultado.sort(
        key=lambda elemento: elemento.get(
            "fecha",
            "",
        ),
        reverse=True,
    )

    return resultado[:MAXIMO_ENTRADAS]


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
    ).text = URL_CNMV

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

    # Verifica que el RSS no esté dañado.
    ET.parse(ARCHIVO_RSS)


def main():
    print(
        "Consultando notificaciones "
        "de directivos de la CNMV..."
    )

    contenido, url_resultados = (
        consultar_cnmv()
    )

    nuevas = extraer_notificaciones(
        contenido,
        url_resultados,
    )

    anteriores = cargar_historial()

    resultado = mezclar_notificaciones(
        nuevas,
        anteriores,
    )

    guardar_historial(resultado)
    crear_rss(resultado)

    print("Proceso finalizado correctamente.")
    print(
        f"Notificaciones encontradas: "
        f"{len(nuevas)}"
    )
    print(
        f"Entradas guardadas: "
        f"{len(resultado)}"
    )
    print(f"URL para Feedly: {URL_RSS}")

    if not nuevas:
        print(
            "AVISO: la consulta ha funcionado, "
            "pero no encontró notificaciones "
            "en el periodo consultado."
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
