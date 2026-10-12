"""Shared prop appearance instructions, separate from per-shot mutable state."""
from __future__ import annotations

import copy
import re
from pydantic import BaseModel, ConfigDict, Field, field_validator


class Prop(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    id: str = Field(pattern=r'^prop_[a-z0-9_]{1,40}$')
    name: str = Field(min_length=1,max_length=80)
    basis: str = Field(min_length=1,max_length=1000)
    appearance: str = Field(min_length=4,max_length=700)
    appearance_en: str = Field(min_length=8,max_length=1000)

    @field_validator('appearance_en')
    @classmethod
    def english_prose(cls,value):
        if any(c in value for c in '<>[]\n\r') or any(c.isalpha() and not c.isascii() for c in value):
            raise ValueError('道具的H3固定外观须为英文描述，不含提示词标签')
        return value


class PropBinding(Prop):
    presence: str = Field(pattern=r'^(opening|later)$')


def catalog(raw, story):
    if not isinstance(raw,list) or len(raw)>8:
        raise ValueError('关键道具设计需为最多8项的列表')
    result=[Prop.model_validate(row).model_dump() for row in raw]
    if len({p['id'] for p in result})!=len(result) or len({p['name'] for p in result})!=len(result):
        raise ValueError('关键道具的标识或名称重复')
    compact=lambda text:re.sub(r'\s+','',text)
    if any(compact(p['basis']) not in compact(story) for p in result):
        raise ValueError('关键道具缺少采用原稿中的连续依据，不能凭空添加')
    return result


def bind(props, references):
    if not isinstance(references,list) or len(references)>8:
        raise ValueError('本镜关键道具绑定无效')
    by_id={p['id']:p for p in props};seen=set();result=[]
    for row in references:
        if (not isinstance(row,dict) or set(row)!={'id','presence'} or not isinstance(row.get('id'),str)
                or row['id'] not in by_id or row['id'] in seen):
            raise ValueError('本镜关键道具引用了未知或重复的身份')
        seen.add(row['id'])
        result.append(PropBinding.model_validate({**by_id[row['id']],'presence':row['presence']}).model_dump())
    return result


def normalize_bindings(rows):
    if not isinstance(rows,list) or len(rows)>8:
        raise ValueError('关键道具绑定格式无效')
    values=[PropBinding.model_validate(p).model_dump() for p in rows]
    if len({p['id'] for p in values})!=len(values):
        raise ValueError('关键道具绑定重复')
    return values


IMAGE_MARKER='\n【关键道具固定外观】'
VIDEO_MARKER=' Object identity constraints: '


def apply(shot, bindings):
    """Do not copy a prop's position/open/closed state across shots or edits."""
    updated=copy.deepcopy(shot)
    bindings=normalize_bindings(bindings)
    if not bindings:return updated
    updated['continuity_props']=bindings
    prompt=str(updated.get('image_prompt') or '').split(IMAGE_MARKER,1)[0]
    notes=[]
    for p in bindings:
        if p['presence']=='opening':
            notes.append(p['name']+'：'+p['appearance'])
        else:
            notes.append(p['name']+'稍后才可见，不提前画入开场。')
    updated['image_prompt']=prompt+IMAGE_MARKER+'\n'+'\n'.join(notes)+'\n仅固定外观和结构，位置、开合、损坏和占用状态服从本镜开场，不提前画后续结果。'
    plan=updated['native_dialogue']
    plan['scene']=plan['scene'].split(VIDEO_MARKER,1)[0]+VIDEO_MARKER+' '.join(p['appearance_en'] for p in bindings)
    from .h3_dialogue import validate_dialogue_draft
    updated['native_dialogue']=validate_dialogue_draft(plan)
    return updated
