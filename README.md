# Al día · Noticias en formato Reels

Esta copia de NewsNest añade un lector de noticias con tarjetas verticales tipo Reels en `/reels`
y una pantalla en español para mantener fuentes y búsquedas en `/reels/manage`.
El menú superior empieza oculto para dar más espacio a las noticias. El botón **Menú**
lo muestra; se cierra con **Cerrar**, Escape, al tocar fuera o al deslizar a otra noticia.
Al cambiar de sección aparece una barra de progreso y un aviso de carga. Los botones
muestran «Guardando…» mientras esperan la respuesta; las reacciones y los guardados
responden visualmente al toque y recuperan su estado anterior si no se confirma el cambio.
Cada noticia tiene **Me interesa** y **No me interesa**. Se conserva una reacción por
noticia, compartida entre sus versiones y reversible al tocar el mismo botón. Las
reacciones ajustan el orden de **Actualidad** cuando se vuelve a abrir, con coincidencias
de palabras del titular, fuente/editor y sección; las coincidencias de titular tienen más
peso. El ajuste es local y acotado, conserva la frescura del ranking y no usa IA ni llamadas
externas. Votar no marca como leída, no borra noticias y no cambia el orden ni los filtros
de la biblioteca de Pendientes. Se reutilizan `items.rating` y `rated_at`, sin migración.
Cambios locales: noviembre de 2026. Se conserva la licencia AGPL-3.0 del proyecto original.
Origen: https://github.com/Lucifix/newsnest · commit `7a149a66d1c47478eb8f360053b28c7e5cbf4a21`.

### Arranque local

Requiere Python 3.12 o posterior. Clona este repositorio en una carpeta de tu elección:

```sh
git clone https://github.com/aldovegavergara-lang/al-dia.git AlDia
cd AlDia
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python run-reels.py
```

Abre http://127.0.0.1:3097/reels. El servidor escucha solo en esta Mac.

### Acceso desde el celular por Tailscale

Con Tailscale conectado, configura el acceso HTTPS privado:

```sh
/Applications/Tailscale.app/Contents/MacOS/Tailscale serve --bg --https=8443 http://127.0.0.1:3097
```

El comando muestra tu dirección privada: `https://<nombre-de-tu-mac>.<tu-tailnet>.ts.net:8443/reels`.

Conecta Tailscale en el celular a la misma red que esta Mac y abre ese enlace en el
navegador. Se mantiene la contraseña del lector. La Mac debe estar activa y conectada.
Tailscale Serve termina HTTPS y redirige al servidor local; está configurado en segundo
plano, sin depender de una terminal abierta. Se usa el puerto 8443 para permitir que otra
aplicación utilice el puerto 443.

Para consultar o restablecer este acceso:

```sh
/Applications/Tailscale.app/Contents/MacOS/Tailscale serve status
/Applications/Tailscale.app/Contents/MacOS/Tailscale serve --bg --https=8443 http://127.0.0.1:3097
```

Para desactivar solamente el acceso remoto del lector:

```sh
/Applications/Tailscale.app/Contents/MacOS/Tailscale serve --https=8443 off
```

### Organización y servicio local

Organización en macOS:

- Código y entorno Python: `~/Develop/AlDia/`.
- Configuración: `~/Library/Application Support/AlDia/config/`.
- Artículos, guardados y estado: `~/Library/Application Support/AlDia/data/`.
- Contraseña local: `~/Library/Application Support/AlDia/data/.local-password`, con permisos privados.
- Registros: `~/Library/Logs/AlDia/service.log` y `error.log`.
- Inicio automático: `~/Library/LaunchAgents/local.aldia.lector.plist`.

La configuración inicial se copia desde `reels-config/` al arrancar por primera vez.
Los cambios hechos desde la interfaz se conservan fuera del código. La contraseña se
genera en el primer arranque: consulta el archivo privado indicado arriba para iniciar sesión.

Servicio local (arranca al iniciar sesión y se recupera si el proceso termina):

```sh
.venv/bin/python scripts/servicio.py install
.venv/bin/python scripts/servicio.py status
.venv/bin/python scripts/servicio.py restart
.venv/bin/python scripts/servicio.py stop
```

`stop` descarga el servicio de esta sesión. Usa `install` para volver a activarlo.

Incluye Cooperativa, BBC Mundo, DW Español y Xataka. Después del primer arranque puedes
añadir el perfil opcional con `.venv/bin/python scripts/configurar-fuentes.py`:
IA y modelos nuevos, modelos chinos, agentes y harness, y Mercado Libre/Mercado Pago.
Combina OpenAI, Hugging Face, The Decoder, tres feeds de Medium, lanzamientos oficiales
de Codex/Claude Code/OpenCode/Hermes Agent y búsquedas temáticas. Desde **Temas y fuentes** puedes añadir
RSS/Atom oficiales o búsquedas de Google News, seleccionar una sección, pausar o quitar fuentes,
editar el contexto de intereses y cambiar la frecuencia de consulta.

Las publicaciones de X llegan mediante búsquedas públicas indexadas y tienen cobertura parcial;
no hay sesión de X conectada. Reddit/LocalLLaMA quedó preparado pero pausado: su RSS respondió
HTTP 404 en la verificación del 22 de noviembre de 2026. No se muestra como una fuente activa.
Los feeds `github.com/owner/repo/releases.atom` usan el lector de versiones estables, que
excluye alphas, betas y otras preversiones. El perfil se puede volver a aplicar con
`.venv/bin/python scripts/configurar-fuentes.py`; conserva fuentes ya existentes.

El recolector comprueba cada minuto qué fuentes están pendientes; por defecto las consulta
cada 15 minutos. Continúa al cerrar el navegador mientras la Mac esté activa y conectada.
El LaunchAgent mantiene el proceso independiente de la terminal y del editor. No usa hosting externo.

La vista inicial **Pendientes** es una biblioteca sin caducidad para las noticias ya recopiladas:
no desaparecen al cumplir 24 horas ni por límites de ranking o por fuente. Se ordenan por
recopilación más antigua primero para retomar la lectura, con páginas de 60 noticias. La lupa
permite combinar texto, tema automático, sección de origen, fuente/búsqueda anterior, fecha
de publicación, reacción y guardados, además del orden. **Leídas** conserva
el historial y permite devolver noticias a pendientes. Abrir la fuente o pulsar **Leída** marca
la noticia; deslizar o guardar no la marca. Los estados sobreviven a reinicios.

**Actualidad** (`/reels?view=latest`) muestra publicaciones de las últimas 24 horas según la
fecha indicada por la fuente, con una opción para ampliar a tres días. Una noticia sin fecha
o con fecha futura no aparece como actualidad. Los artículos más antiguos que el recolector
todavía no hubiera descubierto siguen sujetos al máximo inicial de tres días de las fuentes;
esa restricción no elimina los artículos que ya están en la biblioteca.

La base registra la fuente y URL de búsqueda de cada hallazgo, incluso si la misma URL aparece
en varios seguimientos. Quitar o pausar un seguimiento no borra sus artículos ni el historial.
Los datos previos se incorporaron conservando su origen conocido; no se inventan coincidencias
con búsquedas anteriores que no estuvieran registradas. La migración toma una copia de la
base antes de cambiar su estructura. La última consulta correcta se muestra por fuente en
gestión; es independiente de la hora de actividad del recolector.

Consulta de la biblioteca para asistentes locales, sin modificar lectura ni abrir artículos:

```sh
.venv/bin/python scripts/biblioteca.py --estado pendientes --tema china --buscar DeepSeek
.venv/bin/python scripts/biblioteca.py --estado leidas --limite 20
```

La API autenticada `GET /reels/library` devuelve `counts`, `total`, `next_after` e `items`, con
fechas, estado, reacciones, temas detectados y fuentes. Parámetros: `view=pending|read|saved|all`,
`f` (sección de origen), `topic` (tema automático), `source`, `q`,
`period=0|24|72|168|720` (horas según publicación), `reaction=all|liked|disliked|unrated`,
`exclude` (frases separadas por comas), `bookmarked=1` (solo guardados),
`order=oldest|newest`, `limit` (hasta 100) y `after` (cursor). El cursor evita saltar artículos
al marcar como leída una página completa. La consulta por CLI abre la base en modo de solo lectura.

### Filtros y temas automáticos

Desde **Menú → lupa** combina filtros, usa comillas para frases exactas, `OR` para
alternativas y `-palabra` para exclusiones. Puedes guardar la combinación con un nombre;
queda en la base local y funciona también desde el celular. Una vista guardada filtra
noticias recopiladas; las búsquedas periódicas en Internet se gestionan en Temas y fuentes.

Los temas se detectan por palabras/frases en título, extracto y dirección de la noticia;
no se heredan del nombre de la fuente ni de la consulta de Google News. Una noticia puede
tener varios temas. **¿Por qué estos temas?** muestra las coincidencias. Las reglas se
crean, editan, pausan o eliminan en **Temas y fuentes → Temas automáticos**. Los cambios
se aplican al historial y a los nuevos hallazgos al abrir la vista. Son reglas locales,
no comprensión semántica: los nombres ambiguos pueden requerir exclusiones o ajustes.

Ejemplo de consulta local sin alterar estados:

```sh
.venv/bin/python scripts/biblioteca.py --etiqueta china --ultimas-horas 168 --excluir fútbol
```

Las reglas y vistas se guardan en SQLite, fuera del repositorio. No se borran noticias ni
se marcan leídas al filtrar. El menú permanece oculto por defecto. Más contexto en
[diseño de filtros](docs/reader-filters.md).

Esta versión muestra extractos publicados por los medios. La IA está apagada y el presupuesto
es cero; no necesita claves de pago. Para activar priorización y resúmenes con IA, configura
proveedor/modelo y presupuesto en los ajustes avanzados, activa `scoring.enabled` y arranca con
`NEWSNEST_AI_ENABLED=on`. Las búsquedas temáticas no necesitan IA y envían la consulta a Google News.
Los enlaces de búsquedas pueden pasar por Google News antes de llegar al medio.

Pruebas de la adaptación:

```sh
.venv/bin/python -m pip install pytest
.venv/bin/python -m pytest -q tests/test_reels.py tests/test_reader_filters.py
```

---

<div align="center">

<img src="https://raw.githubusercontent.com/Lucifix/newsnest/main/newsnest/static/icons/icon-192.png" width="96" height="96" alt="NewsNest logo">

# NewsNest

**A self-hosted news feed that learns what you care about.**

NewsNest gathers your RSS feeds into one calm stream, folds the same story from different outlets
into a single card, and uses Claude (or a model of your choice, even a local one) to rank and summarise
what matters to you.

[![Latest release](https://img.shields.io/github/v/release/Lucifix/newsnest)](https://github.com/Lucifix/newsnest/releases/latest)
![License: AGPL-3.0](https://img.shields.io/github/license/Lucifix/newsnest)
[![Docker pulls](https://img.shields.io/docker/pulls/lucifix/newsnest?logo=docker&logoColor=white)](https://hub.docker.com/r/lucifix/newsnest)
[![Build status](https://img.shields.io/github/actions/workflow/status/Lucifix/newsnest/docker-publish.yml?branch=main&label=build)](https://github.com/Lucifix/newsnest/actions/workflows/docker-publish.yml)

[Website](https://lucifix.github.io/newsnest/) · [Demo](https://lucifix.github.io/newsnest/demo/) · [Features](#features) · [Quick start](#quick-start) · [How it works](docs/how-it-works.md) · [Install](docs/install.md) · [Configuration](docs/configuration.md) · [Contributing](CONTRIBUTING.md)

<br>

<table>
  <tr>
    <td align="center" width="33%"><img src="https://raw.githubusercontent.com/Lucifix/newsnest/main/docs/images/feed.png" alt="The For you feed with a top story, key points and a score"></td>
    <td align="center" width="33%"><img src="https://raw.githubusercontent.com/Lucifix/newsnest/main/docs/images/swipe.png" alt="The swipe deck for sorting stories with one thumb"></td>
    <td align="center" width="33%"><img src="https://raw.githubusercontent.com/Lucifix/newsnest/main/docs/images/feed-dark.png" alt="The same feed in dark mode"></td>
  </tr>
  <tr>
    <td align="center"><sub>Feed, ranked for you</sub></td>
    <td align="center"><sub>Swipe to sort</sub></td>
    <td align="center"><sub>Dark mode, automatically</sub></td>
  </tr>
  <tr>
    <td align="center" colspan="3"><img src="https://raw.githubusercontent.com/Lucifix/newsnest/main/docs/images/desktop.png" alt="The home page on a desktop: a front page with the top story and its score, Top stories below it, and the ranked feed beside it"></td>
  </tr>
  <tr>
    <td align="center" colspan="3"><sub>A front page on bigger screens</sub></td>
  </tr>
</table>

<sub>Outlets and headlines are made up. Photos from <a href="https://unsplash.com">Unsplash</a> by
<a href="https://unsplash.com/@jbl12761">James Lee</a>, <a href="https://unsplash.com/@amoltyagi2">Amol Tyagi</a>,
<a href="https://unsplash.com/@gabiontheroad">Gabriella Clare Marino</a> and <a href="https://unsplash.com/@afgprogrammer">Mohammad Rahmani</a>.</sub>

</div>

## Why NewsNest?

Checking a dozen news sites every day is slow, and most of what you see isn't for you. Algorithmic
feeds fix that by learning what keeps you scrolling, which is not the same as what you want to read.

NewsNest is the opposite deal. **You choose the sources, you write down what interests you, and you can
see why every story ranks where it does.** It runs on your own machine, keeps everything in one SQLite
file, and talks to the Anthropic API with your own key, or to any OpenAI-compatible API, including a
local model in Ollama.

Want a look first? Try the [read-only demo](https://lucifix.github.io/newsnest/demo/): made-up stories, no
install.

## Features

- **Your own sources.** Any RSS or Atom feed, GitHub release feeds and OPML import. New feeds go on
  probation and only earn a place once you read them.
- **Ranked for you.** The AI scores each story on topic, importance and personal fit against an
  interests file that you write yourself or have drafted from a few ratings.
- **One story, not five.** The same event from several outlets becomes one card, and wide coverage
  lifts it up.
- **Key points on demand.** A one-line lede, a few bullets, why it matters to you, and whether the full
  article is worth your time.
- **Learns from how you read.** Likes, opens and what you linger on move topics up or down, never out.
  Weekly suggestions for your interests file wait for your OK.
- **No black box.** Every card shows why it ranks where it does, calibration mode shows every score, and
  *Missed this* takes a link the feed should have shown you.
- **Swipe to sort.** A one-thumb deck of the stories NewsNest is least sure about, so a few minutes of
  rating teaches it the most.
- **A hard spending cap.** AI calls stop for the day at your budget (default $0.50). Without an API key
  everything still works, newest first.

<details>
<summary>And more</summary>

- A front page on laptops and desktops: your top story large, then Top stories and a block per category
- An installable web app with light and dark themes, password-protected, with a one-click database backup
- Keyword **watches** that always surface a topic, plus mute and must-show keyword filters
- Full-text **search**, **saved** stories, a **read** history and a day-by-day **archive**
- A status page and a `/health` endpoint that report feeds that fail or go silent
- Per-source daily caps, so a firehose feed can't drown out everything else
- Summaries in the language of your choice

</details>

## Quick start

You need [Docker](https://docs.docker.com/get-docker/) with Compose, and, for the AI features, an
[Anthropic API key](https://console.anthropic.com/) (or another provider, see
[Choosing an AI provider](docs/configuration.md#choosing-an-ai-provider)).

Save this as `docker-compose.yml`, or paste it into Portainer (**Stacks → Add stack → Web editor**). Only
the password needs filling in:

```yaml
services:
  newsnest:
    image: ghcr.io/lucifix/newsnest:latest
    container_name: newsnest
    restart: unless-stopped
    ports:
      - 3095:8080
    environment:
      NEWSNEST_PASSWORD: change-me   # required: with no password the app refuses all access
      ANTHROPIC_API_KEY: ""          # optional: enables ranking and summaries
      TZ: UTC                        # time zone for the logs, e.g. Europe/Ljubljana
    volumes:
      - newsnest-config:/config
      - newsnest-data:/data

volumes:
  newsnest-config:
  newsnest-data:
```

| Setting | What it does |
| --- | --- |
| `3095:8080` | The port you open NewsNest on. Change only the left number, e.g. `8000:8080`. |
| `NEWSNEST_PASSWORD` | **Required.** Password for the web UI. With none set, access is refused. |
| `ANTHROPIC_API_KEY` | Turns on ranking, key points and the weekly suggestions with Claude. |
| `OPENAI_API_KEY` | For OpenAI and other providers, including local models: see [Choosing an AI provider](docs/configuration.md#choosing-an-ai-provider). |
| `TZ` | Time zone for the logs. NewsNest's own time zone is set in **Settings → General**. |
| `/config` | Your feeds, interests and settings. |
| `/data` | The database. |

Then run `docker compose up -d`. The image runs on amd64 and arm64, so a Raspberry Pi or an ARM NAS works
too. **Update:** `docker compose pull && docker compose up -d`.

For host folders instead of Docker volumes, a Portainer stack from the repository, building from source
and backups, see [Installing NewsNest](docs/install.md).

### First run

Open <http://localhost:3095>, log in with your password, and tap **Set up my profile**:

1. **Say where you live.** Your country and city tell the AI whose news is "big", and your time zone is
   picked up from the browser.
2. **Add local news.** NewsNest offers the local pack for your country (Australia, Austria, Canada,
   Croatia, France, Germany, Italy, Slovenia, the United Kingdom and the United States so far), or add your
   own feeds under Local in **Settings → Feeds**.
3. **Rate about 30 headlines**, and the AI drafts your interests from them.

The first fetch only gets what your feeds still hold, usually about a day of news, so the feed fills up
over the first day.

Everything else is in **Settings**, or in the files in `config/`: see
[Configuring NewsNest](docs/configuration.md).

> [!NOTE]
> The login is a single shared password over plain HTTP, which is fine on your home network. If you
> expose NewsNest to the internet, put it behind an HTTPS reverse proxy.

## How it works

NewsNest collects headlines from your feeds, groups the same story from different outlets into one card,
and has an AI score each one against your interests. The best rise to the top of your feed, which keeps
learning from how you read it. [How NewsNest works](docs/how-it-works.md) follows one real headline all
the way to your feed.

**What leaves your server:** headlines and snippets, and for key points the article text, go to your AI
provider with your own key. With a local model in Ollama, nothing leaves your network. Your feed list,
ratings and database stay on your machine, and NewsNest itself sends no telemetry.

## Documentation

- [How NewsNest works](docs/how-it-works.md): one headline's trip from an outlet to your feed
- [Installing NewsNest](docs/install.md): other ways to run it, environment variables, updates, backups,
  installing on your phone
- [Configuring NewsNest](docs/configuration.md): the config files, sections, and other AI providers
  including local models
- [Contributing](CONTRIBUTING.md): development setup, project layout, and an easy first PR

## AI-assisted development

NewsNest started as a personal project, built with the help of AI tools. Eventually, I decided to
open-source it so others could use it too.

## Contributing

Bug reports, ideas and pull requests are welcome. Found a bug? Settings → *About & report a bug* in the
app opens a new issue with your version filled in. Development setup, tests and an easy first PR (a local
news pack for your country) are in [CONTRIBUTING.md](CONTRIBUTING.md).

## License

Copyright © 2026 Lucifix. NewsNest is free software under the
[GNU Affero General Public License v3.0 or later](LICENSE) (AGPL). You may use, change and share it, also
commercially. If you share a changed version, or run one for other people over a network, you must give
them its source code under the same license.

The bundled fonts in [`newsnest/static/fonts/`](newsnest/static/fonts/) have their own license:
[JetBrains Mono](https://github.com/JetBrains/JetBrainsMono) and
[Space Grotesk](https://github.com/floriankarsten/space-grotesk) are licensed under the
[SIL Open Font License 1.1](https://openfontlicense.org); their license files sit next to the fonts.
