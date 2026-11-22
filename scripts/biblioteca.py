"""Read Al día's stored articles for the owner or a local assistant. Makes no changes."""
import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
SUPPORT = Path.home()/'Library'/'Application Support'/'AlDia'
os.environ.setdefault('NEWSNEST_CONFIG_DIR',str(SUPPORT/'config'))
os.environ.setdefault('NEWSNEST_DATA_DIR',str(SUPPORT/'data'))

# The standalone CLI must configure its import path and data location before app imports.
from newsnest.bootstrap import config_dir  # noqa: E402
from newsnest.config import load_config  # noqa: E402
from newsnest.db import session, db_path  # noqa: E402
from newsnest.library import library_page, serialize_page  # noqa: E402
from newsnest.reader_filters import ArticleFilter  # noqa: E402


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--estado',choices=['pendientes','leidas','guardadas','todas'],default='pendientes')
    parser.add_argument('--tema',default='all')
    parser.add_argument('--etiqueta',default='all',help='Tema automático: china, ai, models, harness, meli, releases, economy, chile o unclassified')
    parser.add_argument('--excluir',default='')
    parser.add_argument('--ultimas-horas',type=int,choices=[0,24,72,168,720],default=0)
    parser.add_argument('--reaccion',choices=['all','liked','disliked','unrated'],default='all')
    parser.add_argument('--solo-guardadas',action='store_true')
    parser.add_argument('--fuente',default='')
    parser.add_argument('--buscar',default='')
    parser.add_argument('--orden',choices=['oldest','newest'],default='oldest')
    parser.add_argument('--limite',type=int,default=20)
    parser.add_argument('--despues',type=int,default=0)
    args = parser.parse_args()
    if not db_path().is_file():
        raise SystemExit('La biblioteca aún no está creada. Inicia Al día primero.')
    loaded = load_config(config_dir())
    if loaded.errors:
        raise SystemExit('; '.join(loaded.errors))
    with session() as conn:
        conn.execute('PRAGMA query_only=ON')
        result = library_page(conn,loaded.config,loaded.sources,
                view={'pendientes':'pending','leidas':'read','guardadas':'saved','todas':'all'}[args.estado],
                category=args.tema,source=args.fuente,query=args.buscar,order=args.orden,
                after=max(0,args.despues),limit=args.limite,
                filters=ArticleFilter(topic=args.etiqueta,period=args.ultimas_horas,reaction=args.reaccion,
                                      exclude=args.excluir,bookmarked=args.solo_guardadas,query=args.buscar))
        print(json.dumps(serialize_page(result),ensure_ascii=False,indent=2))
