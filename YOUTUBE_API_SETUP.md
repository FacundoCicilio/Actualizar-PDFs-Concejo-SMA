# YouTube Data API v3: videos del Concejo

## Qué hace

El workflow **Videos del Concejo** (`.github/workflows/concejo-videos.yml`) corre todos los días a las 07:47 de Argentina,
y también a mano desde **Actions**. No toca Instagram, `destacados/` ni `instagram/state.json`.

1. `concejo_videos.py` pide a la API los últimos 15 videos del canal `UCaJbax3QkHRsO4BHKPcxfEw`. Cuesta 2 unidades de cuota.
2. Compara contra `concejo-videos/_index.json` en el dataset de HF.
3. Guarda de cada video nuevo: `video_id`, `title`, `published_at`, `duration` y `description`.
4. Abre un Issue con la etiqueta `nuevo-video-concejo`.

La primera corrida crea el índice con lo que ya está en el canal y no abre Issues.

## Qué no hace: subtítulos

Una API key **no puede bajar subtítulos**. Según la documentación de Google,
[`captions.list`](https://developers.google.com/youtube/v3/docs/captions/list) y
[`captions.download`](https://developers.google.com/youtube/v3/docs/captions/download) piden OAuth con el scope
`youtube.force-ssl` o `youtubepartner`, y `captions.download` además exige permiso para editar el video. El Observatorio
no administra el canal del Concejo. Las alternativas están en `WHISPER_SETUP.md`, si ese archivo está en el repo.

## Configuración

La clave ya está cargada como secret `YOUTUBE_API_KEY`. Para reemplazarla:

1. En <https://console.cloud.google.com/>, elegí el proyecto.
2. Abrí **APIs y servicios > Credenciales** y creá una **Clave de API**.
3. En **Restricciones de API**, dejá solo `YouTube Data API v3`.
4. En el repo, abrí **Settings > Secrets and variables > Actions** y actualizá `YOUTUBE_API_KEY`.

Si la API todavía no está habilitada en un proyecto nuevo, activala en **APIs y servicios > Biblioteca**. Sin el secret, el
script avisa en el log y no hace nada.

## Probar

Corré **Actions > Videos del Concejo > Run workflow**. El log tiene que decir `N videos nuevos; M en el índice.`

## Nota: subtítulos automáticos no confiables

- Los subtítulos auto-generados del canal del Concejo son **NO CONFIABLES**: YouTube a veces clasifica mal el audio como
  inglés. Pasó con el video `9zId93V3yl0` (Sesión 25/26), y tactiq.io, notegpt y la transcripción de YouTube devuelven el
  mismo texto inservible porque usan la misma fuente.
- Para una transcripción confiable, el pipeline debe usar Whisper (API o local) directamente sobre el audio.
- No tiene sentido depender de `captions.download`, aun si consiguiéramos OAuth.
