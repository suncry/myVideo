"""Evidence-based actor profiles and film keywords, scoped to the active library."""
from __future__ import annotations
import collections
import json
import math
import re
import urllib.parse
from typing import Any
import app as core

GENRES = {28:'动作',12:'冒险',16:'动画',35:'喜剧',80:'犯罪',99:'纪录片',18:'剧情',10751:'家庭',14:'奇幻',36:'历史',27:'恐怖',10402:'音乐',9648:'悬疑',10749:'爱情',878:'科幻',10770:'电视电影',53:'惊悚',10752:'战争',37:'西部',10759:'动作冒险',10762:'儿童',10763:'新闻',10764:'真人秀',10765:'科幻奇幻',10766:'肥皂剧',10767:'脱口秀',10768:'战争政治'}
THEMES = {
 '成长':('成长','coming of age','coming-of-age'), '家庭关系':('家庭关系','亲情','family relationship'),
 '友情':('友情','友谊','friendship'), '时间旅行':('时间旅行','穿越时空','time travel'),
 '社会议题':('社会议题','社会现实','social issues','social commentary'), '复仇':('复仇','revenge'),
 '侦探推理':('侦探','推理','detective'), '太空探索':('太空','宇宙探索','space exploration'),
 '公路旅程':('公路','road movie','road trip'), '改编作品':('改编自','based on a novel','adapted from'),
 '女性视角':('女性视角','女性成长','female perspective'), '校园':('校园','school life'),
 '黑色幽默':('黑色幽默','dark comedy','black comedy'), '心理悬疑':('心理悬疑','psychological thriller'),
 '历史人物':('历史人物','biopic','biographical'), '自然生态':('生态','wildlife','nature documentary'),
}
REVIEW_CUES = {'表演受好评':('演技出色','表演出色','精彩表演','excellent performance','great acting','brilliant performance'), '摄影受好评':('摄影出色','摄影精美','画面精美','beautiful cinematography','stunning visuals'), '节奏紧凑':('节奏紧凑','紧凑的节奏','well paced','fast-paced')}


def json_text(value):return json.dumps(value,ensure_ascii=False)


def _work_year(value) -> int | None:
    match=re.search(r'(?:19|20)\d{2}',str(value or ''))
    return int(match.group(0)) if match else None


def _library_work_index(movies: list[dict[str, Any]]) -> dict[str, tuple[int | None, int]]:
    index={}
    for m in movies:
        year=_work_year(m.get('year'))
        for field in ('title','original_title'):
            identity=core.normalize_match_text(m.get(field,''))
            if identity and identity not in index:index[identity]=(year,m['id'])
    return index


def _split_library_works(public_works: list[dict[str, Any]], index: dict[str, tuple[int | None, int]]) -> list[dict[str, Any]]:
    """Mark public works already in the library so browsing only surfaces new discoveries."""
    result=[]
    for w in public_works:
        identity=core.normalize_match_text(w.get('title',''))
        hit=index.get(identity) if identity else None
        if hit:
            stored_year,local_id=hit
            work_year=_work_year(w.get('year'))
            if stored_year is None or work_year is None or abs(stored_year-work_year)<=1:
                w={**w,'in_library':True,'local_id':local_id}
        result.append(w)
    return result


def keywords(text: str, genres=(), refs=()) -> list[dict[str, Any]]:
    text=(text or '').casefold()
    labels=list(dict.fromkeys(str(g).strip() for g in genres if str(g).strip()))[:8]
    result=[dict(label=g,kind='类型',sources=list(refs),evidence='作品公开类型或本地元数据') for g in labels]
    for label,terms in THEMES.items():
        if any(term.casefold() in text for term in terms):
            result.append(dict(label=label,kind='题材',sources=list(refs),evidence='简介或公开关键词中明确提及'))
    return result[:16]


def review_keywords(reviews: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result=[]
    for label,terms in REVIEW_CUES.items():
        matches=[r for r in reviews if any(t in str(r.get('content','')).casefold() for t in terms)
                 and float((r.get('author_details') or {}).get('rating') or 0)>=7
                 and not re.search(r'不.{0,3}(出色|精美|紧凑)|not.{0,12}(great|excellent|beautiful|well paced)',str(r.get('content','')).casefold())]
        if len(matches)>=2 and len(matches)/max(1,len(reviews))>=0.5:
            result.append(dict(label=label,kind='评论样本',sources=[r.get('url','') for r in matches if r.get('url')],evidence=f'{len(reviews)} 条评论样本中 {len(matches)} 条明确赞同；仅代表已获取样本'))
    return result


def movie_insights(movie: dict[str, Any]) -> dict[str, Any]:
    saved=core.json_value(movie.get('insights_json'),{})
    refs=[{'label':'本地影片资料','url':''}]
    tags=keywords(str(movie.get('overview') or '')+' '+' '.join(movie.get('tags') or []), movie.get('genres') or [], refs)
    combined={k['label']:k for k in tags}
    for item in saved.get('keywords',[]):combined[item['label']]=item
    return {**saved,'keywords':list(combined.values())[:16]}


def actor_profile(name: str, *, _rows=None, _movies=None) -> dict[str, Any] | None:
    key=core.normalize_actor_name(name)
    with core.connect() as conn:
        rows=_rows if _rows is not None else [dict(r) for r in conn.execute('SELECT * FROM actor_profiles')]
        row=next((r for r in rows if key==r['name_key']),None)
        if row is None:
            matches=[r for r in rows if key in [core.normalize_actor_name(x) for x in core.json_value(r.get('aliases_json'),[])]]
            row=matches[0] if len(matches)==1 else None
        if not row:return None
        movies=_movies if _movies is not None else [core.movie_dict(r) for r in conn.execute("SELECT * FROM movies WHERE file_status<>'trashed'")]
    aliases={core.normalize_actor_name(x) for x in [row['name'],row.get('display_name') or '',*core.json_value(row.get('aliases_json'),[])] if x}
    local=[m for m in movies if aliases.intersection(core.normalize_actor_name(p.get('name','')) for p in m.get('cast',[]))]
    data=core.json_value(row.get('insights_json'),{})
    known_aliases=core.unique_actor_names([*core.json_value(row.get('aliases_json'),[]),*data.get('aliases',[])])
    public_works=_split_library_works(data.get('works',[]),_library_work_index(movies))
    discoverable_works=[w for w in public_works if not w.get('in_library')]
    # Collapse genre evidence across multiple works; no personality or appearance inferences.
    unique_works={}
    for work in [*public_works,*local]:
        identity=core.normalize_match_text(work.get('title',''))
        unique_works.setdefault(identity,set()).update(work.get('genres',[]))
    counts=collections.Counter(g for genres in unique_works.values() for g in genres)
    actor_tags=[dict(label=g,kind='作品类型',evidence=f'{n} 部已收录作品涉及此类型',sources=data.get('sources',[])) for g,n in counts.most_common(7)]
    theme_counts=collections.Counter(k['label'] for m in local for k in movie_insights(m)['keywords'] if k['kind']=='题材')
    actor_tags.extend(dict(label=g,kind='作品题材',evidence=f'{n} 部资料库作品的简介明确提及',sources=[{'label':'本地影片资料','url':''}]) for g,n in theme_counts.most_common(4))
    existing={x['label'] for x in actor_tags}
    actor_tags.extend(x for x in data.get('keywords',[]) if x['label'] not in existing)
    acclaim=data.get('acclaim') or {}
    reception_tags=[]
    if acclaim.get('rated_count') and acclaim.get('high_rated'):
        reception_tags.append(dict(label=f"高分代表作 {acclaim['high_rated']} 部",kind='大众评价',evidence=f"{acclaim['rated_count']} 部评分人数不少于 50 的公开作品中，{acclaim['high_rated']} 部评分达 8.0 以上",sources=list(data.get('sources',[]))))
    for label in acclaim.get('performance_labels',[]):
        reception_tags.append(dict(label=label,kind='大众评价',evidence=acclaim.get('performance_evidence') or '公开评论样本中明确赞同',sources=list(acclaim.get('performance_sources') or [])))
    return {**row,'favorite':bool(row.get('favorite')),'avatar':row.get('avatar_url',''),'aliases':known_aliases,'info':{**core.json_value(row.get('info_json'),{}),**data.get('info',{})},'local_works':local,'works':discoverable_works,'keywords':(reception_tags+actor_tags)[:12],'sources':data.get('sources',[]),'warnings':data.get('warnings',[]),'fetched_at':data.get('fetched_at',''),'biography':data.get('biography') or row.get('biography') or '','acclaim':acclaim,'awards':[str(a) for a in data.get('awards') or []],'insights':data}


def actor_list(query='', favorite_only=False, local_only=False) -> list[dict[str, Any]]:
    with core.connect() as c:
        rows=[dict(r) for r in c.execute('SELECT * FROM actor_profiles')]
        movies=[core.movie_dict(r) for r in c.execute("SELECT * FROM movies WHERE file_status<>'trashed'")]
    profiles=[p for row in rows if (p:=actor_profile(row['name'],_rows=rows,_movies=movies))]
    q=query.casefold().strip()
    return sorted([p for p in profiles if (not favorite_only or p['favorite']) and (not local_only or p['local_works']) and (not q or q in ' '.join([p['name'],p.get('display_name') or '',*p['aliases'],*[x['label'] for x in p['keywords']]]).casefold())],key=lambda p:(not p['favorite'],-len(p['local_works']),p['name'].casefold()))


def set_actor_favorite(name: str, value: bool) -> None:
    p=actor_profile(name)
    if not p:raise ValueError('演员资料不存在，请先添加公开资料。')
    with core.connect() as c:c.execute('UPDATE actor_profiles SET favorite=?,retained=1 WHERE name_key=?',(int(value),p['name_key']))


def save_public_actor(profile: dict[str, Any]) -> str:
    name=str(profile.get('name') or '').strip()
    if not name:raise ValueError('公开资料缺少姓名')
    key=core.normalize_actor_name(name)
    with core.connect() as c:
        previous=c.execute('SELECT avatar_url,insights_json FROM actor_profiles WHERE name_key=?',(key,)).fetchone()
        if previous:
            from actor_media import merge_gallery
            profile=merge_gallery(core.json_value(previous['insights_json'],{}),profile)
        selected=previous['avatar_url'] if previous and previous['avatar_url'] else profile.get('avatar','')
        c.execute("""INSERT INTO actor_profiles(name_key,name,display_name,avatar_url,source,source_id,aliases_json,biography,info_json,source_refs_json,status,retained,insights_json,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,1,?,?) ON CONFLICT(name_key) DO UPDATE SET
        retained=1,insights_json=excluded.insights_json,biography=CASE WHEN excluded.biography<>'' THEN excluded.biography ELSE actor_profiles.biography END,
        avatar_url=CASE WHEN actor_profiles.avatar_url='' THEN excluded.avatar_url ELSE actor_profiles.avatar_url END,updated_at=excluded.updated_at""",
        (key,name,profile.get('display_name') or name,profile.get('avatar',''),profile.get('source','public'),str(profile.get('source_id') or ''),json_text(profile.get('aliases',[])),profile.get('biography',''),json_text(profile.get('info',{})),json_text({s.get('label','public'):s['url'] for s in profile.get('sources',[]) if s.get('url')}),'matched',json_text(profile),core.now_iso()))
    if profile.get('photos') or profile.get('avatar'):
        core.store_actor_photos(key,profile.get('photos',[]),selected)
    return key


def local_recommendations(name: str, candidates=None) -> list[dict[str, Any]]:
    target=actor_profile(name)
    if not target:return []
    labels={t['label'] for t in target['keywords'] if t['kind']!='职业'}
    if not labels:return []
    pool=candidates if candidates is not None else actor_list()
    output=[]
    for candidate in pool:
        if candidate['name_key']==target['name_key']:continue
        other={t['label'] for t in candidate['keywords'] if t['kind']!='职业'}
        shared=sorted(labels & other)
        if not shared:continue
        score=len(shared)/len(labels|other)
        status='资料库尚无作品' if not candidate.get('local_works') else f"资料库已有 {len(candidate['local_works'])} 部"
        output.append({**candidate,'shared_keywords':shared,'similarity':score,'reason':'共同标签：'+'、'.join(shared[:4])+' · '+status})
    # Rank actors without library works first: browsing recommendations aims at discovery, not repeats.
    return sorted(output,key=lambda p:(-p['similarity'],bool(p['local_works']),-len(p.get('works',[]))))[:16]


def refresh_actor(name: str) -> dict[str, Any]:
    from public_sources import fetch_actor
    existing=actor_profile(name)
    if not existing:raise ValueError('演员不存在')
    public=fetch_actor(existing)
    public['name']=existing['name'];public['display_name']=public.get('display_name') or existing.get('display_name') or existing['name']
    if not public.get('biography') and not public.get('works') and not public.get('sources'):
        raise ValueError('暂未取得可核对的公开资料。现有照片、收藏和介绍已保留。')
    save_public_actor(public)
    return actor_profile(name)


def refresh_actor_media(name: str) -> dict[str, Any]:
    from actor_media import fetch_gallery
    p=actor_profile(name)
    if not p:raise ValueError('演员不存在')
    if not p['insights'].get('works') and not p.get('fetched_at'):p=refresh_actor(name)
    gallery=fetch_gallery(p)
    # Store artwork separately from personal favorites, identity, and chosen portrait.
    with core.connect() as c:c.execute('UPDATE actor_profiles SET insights_json=? WHERE name_key=?',(json_text(gallery),p['name_key']))
    core.store_actor_photos(p['name_key'],gallery.get('photos',[]),p.get('avatar',''))
    return actor_profile(name)


def search_public_actors(query: str) -> list[dict[str, Any]]:
    from public_sources import search_actors
    return search_actors(query.strip())


def add_public_actor(candidate: dict[str, Any]) -> dict[str, Any]:
    from public_sources import fetch_actor
    public=fetch_actor(candidate)
    if not public.get('name'):raise ValueError('无法取得演员公开资料，请稍后重试。')
    save_public_actor(public)
    return actor_profile(public['name'])


def discover_similar(name: str) -> dict[str, Any]:
    from public_sources import similar_candidates,fetch_actor,PublicClient
    p=actor_profile(name)
    if not p:raise ValueError('演员不存在')
    if not p.get('fetched_at'):p=refresh_actor(name)
    candidates,warnings=similar_candidates(p)
    client=PublicClient();checked=0
    for candidate in candidates[:10]:
        existing=actor_profile(candidate['name'])
        if existing and existing.get('fetched_at') and existing.get('source')==candidate.get('source') and str(existing.get('source_id'))==str(candidate.get('source_id')):
            checked+=1
            continue
        checked+=1
        try:
            public=fetch_actor(candidate,client)
            if public.get('works') or public.get('keywords'):save_public_actor(public)
        except (OSError,ValueError) as exc:
            if getattr(exc,'code',None)==429:
                warnings.append('公开资料来源正在限流，请稍后继续；已取得的资料和推荐会保留')
                break
            warnings.append('部分候选演员公开资料暂未取得')
    return {'recommendations':local_recommendations(name),'warnings':list(dict.fromkeys(warnings)),'candidates_checked':checked}


def refresh_movie(movie_id: int) -> dict[str, Any]:
    from public_sources import fetch_movie
    with core.connect() as c:row=c.execute('SELECT * FROM movies WHERE id=?',(movie_id,)).fetchone()
    if not row:raise ValueError('影片不存在')
    movie=core.movie_dict(row);public=fetch_movie(movie)
    with core.connect() as c:c.execute('UPDATE movies SET insights_json=? WHERE id=?',(json_text(public),movie_id))
    return movie_insights({**movie,'insights_json':json_text(public)})
