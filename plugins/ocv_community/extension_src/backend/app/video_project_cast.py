"""User-confirmed project identities bound to exact, project-owned image bytes.

A binding is reusable identity evidence, not image recognition or confirmation
that the character appears in any shot. Names never select or discover assets.
The caller owns authorization, revision checks and persistence of ``project_cast``.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import uuid
from pathlib import Path, PureWindowsPath

from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, field_validator


VERSION = 1
SOURCE = 'user_confirmed_project_identity'
AUTO_SOURCE = 'auto_designed_project_identity'
_EDIT_FIELDS = ('id', 'name', 'reference_id', 'constraints')
_IDENTITY = re.compile(r'pc_[a-f0-9]{32}\Z')
_DIGEST = re.compile(r'[a-f0-9]{64}\Z')
_CONTROL = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ud800-\udfff]')


class ProjectCastDeclaration(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    id: str = Field(default='', max_length=160)
    name: str = Field(min_length=1, max_length=160)
    reference_id: str = Field(min_length=1, max_length=160)
    constraints: str = Field(default='', max_length=2000)

    @field_validator('id', mode='before')
    @classmethod
    def empty_identity(cls, value):
        return '' if value is None else value

    @field_validator('id')
    @classmethod
    def project_identity(cls, value):
        if value and not _IDENTITY.fullmatch(value):
            raise ValueError('项目角色标识无效，请从本项目已保存角色中选择')
        return value

    @field_validator('name', 'reference_id', 'constraints')
    @classmethod
    def ordinary_text(cls, value):
        if _CONTROL.search(value):
            raise ValueError('角色名称、参考图标识或约束中含无效字符')
        return value


class ProjectCastMutation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: int = Field(ge=0, strict=True)
    # Omission must not silently clear the saved cast.
    characters: list[ProjectCastDeclaration] = Field(max_length=16)

    @field_validator('characters')
    @classmethod
    def unique_identities(cls, values):
        identities = [row.id for row in values if row.id]
        if len(identities) != len(set(identities)):
            raise ValueError('项目角色标识不能重复')
        return values


def _scope(owner, record):
    if (type(owner) is not int or owner < 1 or not isinstance(record, dict)
            or not isinstance(record.get('id'), str) or not record['id'].strip()):
        raise ValueError('项目角色的账号或项目标识无效')
    return record['id']


def _saved_characters(owner, record):
    project = _scope(owner, record)
    saved = record.get('project_cast')
    if saved is None:
        return []
    if (not isinstance(saved, dict) or type(saved.get('version')) is not int
            or saved['version'] != VERSION or saved.get('source') not in {SOURCE, AUTO_SOURCE}
            or type(saved.get('owner_id')) is not int or saved['owner_id'] != owner
            or saved.get('project_id') != project):
        raise ValueError('项目角色记录与当前账号或项目不一致，请重新保存角色设置')
    rows = saved.get('characters')
    if not isinstance(rows, list) or len(rows) > 16:
        raise ValueError('项目角色记录格式无效，请重新保存角色设置')
    result, identities = [], set()
    for raw in rows:
        try:
            if not isinstance(raw, dict):
                raise ValueError('Invalid character record')
            row = ProjectCastDeclaration.model_validate({key: raw[key] for key in _EDIT_FIELDS if key in raw}).model_dump()
            if not row['id'] or row['id'] in identities:
                raise ValueError('Invalid character identity')
        except ValueError as exc:
            raise ValueError('项目角色记录格式无效，请重新保存角色设置') from exc
        identities.add(row['id'])
        for key in ('reference_sha256', 'identity_sha256'):
            value = raw.get(key)
            row[key] = value if isinstance(value, str) and _DIGEST.fullmatch(value) else ''
        result.append(row)
    return result


def _image_digest(path, catalog, identity):
    if not isinstance(catalog, (list, tuple)):
        raise ValueError('本项目参考图目录无效，请重新读取项目')
    matches = [row for row in catalog if isinstance(row, dict) and row.get('id') == identity]
    if len(matches) != 1:
        raise ValueError('角色参考图不存在或标识重复，请重新选择本项目图片')
    relative = matches[0].get('file')
    if not isinstance(relative, str) or not relative or _CONTROL.search(relative):
        raise ValueError('角色参考图文件无效，请重新选择本项目图片')
    relative = relative.replace('\\', '/')
    local = Path(relative)
    if (local.is_absolute() or PureWindowsPath(relative).drive or '..' in local.parts or ':' in relative):
        raise ValueError('角色参考图必须属于本项目，不能引用外部文件')
    try:
        root = Path(path).resolve()
        target = (root / local).resolve()
        if root not in target.parents or not target.is_file():
            raise ValueError('角色参考图已删除或不属于本项目，请重新选择')
        data = target.read_bytes()
    except (OSError, RuntimeError, TypeError) as exc:
        raise ValueError('角色参考图暂时无法读取，请检查图片后重试') from exc
    try:
        # Verify exactly the bytes hashed below; do not reopen a changing path.
        with Image.open(io.BytesIO(data)) as image:
            image.verify()
    except Exception as exc:
        raise ValueError('角色绑定需要有效的图片文件，请重新上传或选择图片') from exc
    return hashlib.sha256(data).hexdigest()


def _identity_digest(row, reference_sha256):
    basis = {key: row[key] for key in _EDIT_FIELDS}
    basis['reference_sha256'] = reference_sha256
    return hashlib.sha256(json.dumps(basis, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()


def build_project_cast(owner, path, record, catalog, declarations, *, source=SOURCE):
    """Return a new saved object; do not mutate the project or write any files.

    Only empty IDs create characters. Explicit IDs must already belong to this
    project's saved cast, including stale rows the user is explicitly repairing.
    An explicit empty list clears the cast, including a malformed prior record.
    """
    if source not in {SOURCE, AUTO_SOURCE}:
        raise ValueError('项目角色来源无效')
    project = _scope(owner, record)
    try:
        values = ProjectCastMutation(revision=0, characters=declarations).characters
    except ValueError as exc:
        raise ValueError('项目角色填写无效：需提供名称、本项目参考图及不超过2000字的约束，最多16名且标识不能重复') from exc
    existing = {row['id'] for row in _saved_characters(owner, record)} if values else set()
    rows, images = [], {}
    for declaration in values:
        row = declaration.model_dump()
        if row['id'] and row['id'] not in existing:
            raise ValueError('角色标识不属于本项目，请选择已有项目角色或留空新增')
        if not row['id']:
            row['id'] = 'pc_' + uuid.uuid4().hex
        identity = row['reference_id']
        if identity not in images:
            images[identity] = _image_digest(path, catalog, identity)
        row['reference_sha256'] = images[identity]
        row['identity_sha256'] = _identity_digest(row, images[identity])
        rows.append(row)
    return {'version': VERSION, 'source': source, 'owner_id': owner,
            'project_id': project, 'characters': rows}


def project_cast_view(owner, path, record, catalog):
    """Return editable labels and verified tokens, never filesystem paths.

    A stale row has no usable identity token. Each row is checked independently;
    changing one reference does not invalidate another character's binding.
    """
    rows, images = [], {}
    for saved in _saved_characters(owner, record):
        row = dict(saved)
        identity = row['reference_id']
        if identity not in images:
            try:
                images[identity] = (_image_digest(path, catalog, identity), '')
            except ValueError as exc:
                images[identity] = ('', str(exc))
        current, error = images[identity]
        valid = bool(current and current == row['reference_sha256']
                     and row['identity_sha256'] == _identity_digest(row, current))
        row['status'] = 'current' if valid else 'stale'
        row['message'] = ('已保存此角色与参考图的身份绑定；出场情况仍需逐镜确认。' if valid else
                          error or '角色资料或参考图已变化，请核对后重新保存项目角色。')
        if not valid:
            row['identity_sha256'] = ''
        rows.append(row)
    return {'characters': rows}
