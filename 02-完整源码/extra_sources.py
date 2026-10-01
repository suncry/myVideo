"""Additional ordinary film/anime credits from documented public APIs."""
import re,urllib.parse
import app as core


def person_aliases(person):
    aliases=[person.get('name',''),person.get('name_cn','')]
    info={}
    for item in person.get('infobox') or []:
        key=item.get('key','');value=item.get('value')
        if key in ('别名','简体中文名'):
            if isinstance(value,list):aliases.extend(core.flatten_profile_value(v) for v in value)
            else:aliases.append(core.flatten_profile_value(value))
        elif key and value:info[key]=core.flatten_profile_value(value)
    return core.unique_actor_names(aliases),info


def search_extra(query,c):
    results=[]
    try:
        data=c.post('https://api.bgm.tv/v0/search/persons?limit=8',{'keyword':query})
        for person in data.get('data',[]):
            career=person.get('career') or []
            if career and not any(x in career for x in ('seiyu','actor')):continue
            aliases,info=person_aliases(person)
            results.append(dict(name=person.get('name',''),display_name=core.chinese_display_name(aliases),aliases=aliases,source='bangumi-person',source_id=str(person['id']),description='Bangumi · 人物与配音资料',avatar=(person.get('images') or {}).get('large') or ''))
    except (OSError,ValueError):pass
    try:
        data=c.get('https://api.jikan.moe/v4/people?'+urllib.parse.urlencode({'q':query,'limit':8}))
        for person in data.get('data',[]):
            results.append(dict(name=person.get('name',''),aliases=[str(person.get('given_name') or '')+' '+str(person.get('family_name') or ''),*(person.get('alternate_names') or [])],source='mal-person',source_id=str(person['mal_id']),description='MyAnimeList / Jikan · 配音资料候选，添加时核对作品',avatar=(person.get('images') or {}).get('jpg',{}).get('image_url','')))
    except (OSError,ValueError):pass
    try:
        data=c.get('https://api.tvmaze.com/search/people?'+urllib.parse.urlencode({'q':query}))
        for item in data[:8] if isinstance(data,list) else []:
            p=item.get('person',{})
            if not p.get('name'):continue
            results.append(dict(name=p['name'],source='tvmaze-person',source_id=str(p['id']),description='TVmaze · 电视剧演职员资料',avatar=(p.get('image') or {}).get('original') or ''))
    except (OSError,ValueError):pass
    return results


def bangumi_actor(c,sid):
    p=c.get('https://api.bgm.tv/v0/persons/'+str(sid));aliases,info=person_aliases(p);works=[]
    # Subject relations are already tied to the confirmed person ID.
    characters=c.get(f'https://api.bgm.tv/v0/persons/{sid}/characters')
    relations=[];seen=set()
    for character in sorted(characters if isinstance(characters,list) else [],key=lambda r:r.get('staff')!='主角'):
        wid=str(character.get('subject_id') or '')
        if character.get('subject_type') not in (2,6) or not wid.isdigit() or wid in seen:continue
        seen.add(wid);relations.append(dict(id=wid,name=character.get('subject_name',''),name_cn=character.get('subject_name_cn',''),staff='配音'))
        if len(relations)>=18:break
    for r in relations[:40] if isinstance(relations,list) else []:
        relation=r.get('staff') or r.get('relation') or ''
        if relation and not any(x in str(relation) for x in ('配音','声优','演出','演员','出演')):continue
        subject=r.get('subject') or r;wid=str(subject.get('id') or r.get('id') or '')
        if not wid.isdigit():continue
        title=subject.get('name_cn') or subject.get('name','')
        if subject.get('nsfw') is True:continue
        # Relations lack a content flag on some API versions: inspect actual subjects.
        if subject.get('nsfw') is None:
            if len(works)>=12:break
            try:subject=c.get('https://api.bgm.tv/v0/subjects/'+wid)
            except (OSError,ValueError):continue
            if subject.get('nsfw') is not False:continue
            title=subject.get('name_cn') or subject.get('name','')
        if not title:continue
        genres=[t.get('name','') for t in (subject.get('tags') or [])[:12] if isinstance(t,dict) and t.get('name') in {'剧情','喜剧','搞笑','动作','战斗','科幻','奇幻','悬疑','冒险','恋爱','爱情','音乐','历史','校园','日常','运动','治愈'}]
        rating=subject.get('rating') or {}
        works.append(dict(id=wid,media_type='anime',title=title,year=str(subject.get('date',''))[:4],genres=genres,poster=(subject.get('images') or {}).get('large') or '',source='Bangumi',url='https://bgm.tv/subject/'+wid,vote_average=float(rating.get('score') or 0),vote_count=int(rating.get('total') or 0),popularity=sum(v for v in (subject.get('collection') or {}).values() if isinstance(v,int))))
    if not works:raise ValueError('此 Bangumi 人物暂未取得可确认的配音或出演作品。')
    works=sorted({w['id']:w for w in works}.values(),key=lambda w:(w.get('popularity',0),w.get('vote_count',0)),reverse=True)
    avatar=(p.get('images') or {}).get('large') or ''
    return dict(name=p.get('name',''),display_name=core.chinese_display_name(aliases),aliases=aliases,source='bangumi-person',source_id=str(sid),avatar=avatar,photos=[dict(photo_url=avatar,source='Bangumi',source_url='https://bgm.tv/person/'+str(sid))] if avatar else [],biography=p.get('summary',''),info=info,works=works,sources=[dict(label='Bangumi · 人物与配音作品',url='https://bgm.tv/person/'+str(sid))])


def mal_actor(c,sid):
    p=c.get(f'https://api.jikan.moe/v4/people/{sid}/full').get('data',{});roles=c.get(f'https://api.jikan.moe/v4/people/{sid}/voices').get('data',[]);works=[]
    # Only anime with non-explicit content metadata are eligible.
    for role in roles[:20]:
        anime=role.get('anime') or {};wid=str(anime.get('mal_id') or '')
        if not wid.isdigit():continue
        try:detail=c.get('https://api.jikan.moe/v4/anime/'+wid).get('data',{})
        except (OSError,ValueError):continue
        if str(detail.get('rating','')).startswith('Rx') or any(g.get('name') in ('Hentai','Erotica') for g in detail.get('genres',[])):continue
        if not detail.get('rating'):continue
        title=detail.get('title') or anime.get('title','')
        if not title:continue
        works.append(dict(id=wid,media_type='anime',title=title,year=str(detail.get('year') or ''),genres=[g['name'] for g in detail.get('genres',[])],poster=detail.get('images',{}).get('jpg',{}).get('large_image_url') or anime.get('images',{}).get('jpg',{}).get('image_url',''),source='MyAnimeList / Jikan',url=detail.get('url') or anime.get('url',''),vote_average=float(detail.get('score') or 0),vote_count=int(detail.get('scored_by') or 0),popularity=detail.get('members') or 0))
        if len(works)>=10:break
    if not works:raise ValueError('此人物暂未取得可确认的公开配音作品。')
    works=sorted({w['id']:w for w in works}.values(),key=lambda w:(w.get('popularity',0),w.get('vote_count',0)),reverse=True)
    avatar=p.get('images',{}).get('jpg',{}).get('image_url','');birthday=str(p.get('birthday') or '')[:10]
    return dict(name=p.get('name',''),aliases=[*p.get('alternate_names',[]),str(p.get('given_name') or '')+' '+str(p.get('family_name') or '')],source='mal-person',source_id=str(sid),avatar=avatar,photos=[dict(photo_url=avatar,source='MyAnimeList / Jikan',source_url=p.get('url',''))] if avatar else [],biography=p.get('about',''),info={'生日':birthday} if birthday else {},works=works,sources=[dict(label='MyAnimeList / Jikan · 人物与配音作品',url=p.get('url',''))])


def tvmaze_actor(c,sid):
    p=c.get('https://api.tvmaze.com/people/'+str(sid));credits=c.get(f'https://api.tvmaze.com/people/{sid}/castcredits?embed=show');works=[]
    for credit in credits if isinstance(credits,list) else []:
        show=credit.get('_embedded',{}).get('show',{})
        if not show.get('name') or any(g.casefold() in ('adult','erotica','pornographic') for g in show.get('genres',[])):continue
        works.append(dict(id=str(show['id']),media_type='tvmaze-tv',title=show['name'],year=str(show.get('premiered') or '')[:4],genres=show.get('genres',[]),poster=(show.get('image') or {}).get('original') or '',source='TVmaze',url=show.get('url',''),popularity=show.get('weight') or 0))
    works=sorted({w['id']:w for w in works}.values(),key=lambda w:w['popularity'],reverse=True)[:60]
    if not works:raise ValueError('此 TVmaze 人物暂未取得可确认的电视剧出演作品。')
    avatar=(p.get('image') or {}).get('original') or '';url=p.get('url','')
    return dict(name=p['name'],aliases=[],source='tvmaze-person',source_id=str(sid),avatar=avatar,photos=[dict(photo_url=avatar,source='TVmaze',source_url=url)] if avatar else [],biography='',info={k:v for k,v in {'生日':p.get('birthday')}.items() if v},works=works,sources=[dict(label='TVmaze · 人物与电视剧作品',url=url)])
