# Filtros y clasificación de Al día

La sección de un RSS describe la suscripción, pero un medio general también publica sobre
IA o Mercado Libre. El lector mantiene esa sección y añade temas independientes según el
contenido de cada noticia. Las versiones de la misma noticia comparten la tarjeta; las
coincidencias de todas sus versiones contribuyen a los temas detectados.

Las reglas combinan alternativas literales separadas por comas y exclusiones. Se comparan
palabras o frases completas, ignorando mayúsculas y tildes. El texto se limpia de etiquetas
HTML. La URL aporta dominio y ruta, útil en notas de versiones cuyo título es solo un número;
la consulta y el fragmento de la URL no aportan evidencia. Cada etiqueta expone las palabras
que coincidieron. Ninguna coincidencia produce «Sin clasificar». No hay llamadas a modelos,
traducción automática ni garantía de comprender el significado de un nombre ambiguo.

La interfaz permite combinar estado de lectura, tema, sección de origen, fuente o búsqueda,
texto, ventana de publicación, reacción y guardados. La consulta textual usa AND por defecto,
comillas para frases, `OR` para alternativas y `-palabra` para excluir. Un campo separado
excluye cualquiera de las frases indicadas. Las fechas de publicación desconocidas o futuras
no pasan una ventana temporal; filtrar nunca borra artículos. Actualidad conserva su ventana
de 24/72 horas, selección por frescura y orden personalizado. La biblioteca conserva su cursor
por identificador, independiente del estado de lectura.

Las vistas guardadas almacenan solo filtros admitidos, nunca una URL externa o un cursor de
paginación. Se comparten entre dispositivos que usan la misma instalación. No crean nuevas
suscripciones ni cambian la frecuencia del recolector. Se pueden quitar sin perder noticias.
Las reglas y vistas residen en `meta` de la base SQLite local (`aldia.topic_rules.v1` y
`aldia.saved_views.v1`); no requieren una migración ni copian preferencias al código público.

Se adoptaron patrones documentados por otros lectores:

- [FreshRSS: filtros combinables, búsqueda por frases, exclusiones y consultas guardadas](https://freshrss.github.io/FreshRSS/en/users/10_filter.html).
- [Feedly: exclusiones por palabra o frase](https://docs.feedly.com/article/109-how-can-i-add-create-a-mute-filter).

Las comprobaciones cubren clasificación múltiple, términos ambiguos, reglas editables,
reclasificación del historial, filtros combinados, fechas desconocidas, persistencia de vistas,
protección de acceso, reacciones reversibles y paginación estable tras marcar una página leída.
