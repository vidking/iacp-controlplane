"""Comparación estricta del contrato HTTP excluyendo solo anotaciones editoriales."""
from copy import deepcopy
import argparse
import json
from pathlib import Path


def transport_projection(schema):
    result = deepcopy(schema)
    result['info'] = {'version': result['info']['version']}
    result.pop('tags', None)
    result.pop('externalDocs', None)
    def media_annotations(node):
        for media in node.get('content', {}).values():
            media.pop('examples', None);media.pop('example', None)
    for methods in result['paths'].values():
        for operation in methods.values():
            for key in ('summary','description','tags','externalDocs'):operation.pop(key, None)
            for parameter in operation.get('parameters', []):
                for key in ('description','examples','example'):parameter.pop(key, None)
            body = operation.get('requestBody', {})
            body.pop('description', None);media_annotations(body)
            for response in operation.get('responses', {}).values():
                response.pop('description', None);media_annotations(response)
    # Components, schemas, defaults, required, security y todos los códigos quedan exactos.
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('before',type=Path);p.add_argument('after',type=Path)
    args=p.parse_args();a,b=[json.loads(f.read_text()) for f in [args.before,args.after]]
    passed=transport_projection(a)==transport_projection(b)
    print(json.dumps({'contrato_http_identico':passed,'anotaciones_editoriales_excluidas':True},ensure_ascii=False))
    raise SystemExit(0 if passed else 1)


if __name__=='__main__':main()
