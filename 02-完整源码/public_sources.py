"""Official public film/encyclopedia APIs; explicit identities and bounded requests."""
from __future__ import annotations
import collections
import re
import time
import urllib.parse
import urllib.error
from typing import Any
import app as core


class PublicClient:
    def __init__(self):self.deadline=time.monotonic()+100;self.requests=0;self.entity_cache={};self.host_times={}
    def get(self,url,headers=None):
        for attempt in range(2):
            if time.monotonic()>self.deadline or self.requests>=60:raise ValueError('本次公开资料查询已达到限额，请稍后继续。')
            host=urllib.parse.urlsplit(url).hostname
            if host=='api.jikan.moe':
                delay=max(0,0.4-(time.monotonic()-self.host_times.get(host,0)))
                if delay:time.sleep(delay)
                self.host_times[host]=time.monotonic()
            self.requests+=1
            try:return core.http_json(url,headers=headers)
            except urllib.error.HTTPError as exc:
                if attempt or exc.code not in (429,500,502,503,504):raise
                try:delay=float(exc.headers.get('Retry-After','2'))
                except (TypeError,ValueError,AttributeError):delay=2
                time.sleep(min(8,max(1,delay)))
            except OSError:
                if attempt:raise
    def post(self,url,body):
        if time.monotonic()>self.deadline or self.requests>=60:raise ValueError('本次公开资料查询已达到限额，请稍后继续。')
        self.requests+=1
        return core.http_json(url,method='POST',body=body)
    def tmdb(self,path,**params):
        token=core.setting_value('tmdb_token')
        if not token:raise ValueError('完整作品和评论资料需要在系统设置中配置 TMDb Token。百科资料仍可使用。')
        return self.get('https://api.themoviedb.org/3/'+path+'?'+urllib.parse.urlencode({'language':'zh-CN',**params}),{'Authorization':f'Bearer {token}'})
    def wiki(self,**params):
        return self.get('https://www.wikidata.org/w/api.php?'+urllib.parse.urlencode({'format':'json',**params}))
    def search_claims(self,query,limit=24):
        result=self.wiki(action='query',list='search',srsearch=query,srnamespace=0,srlimit=limit,srprop='')
        return [r['title'] for r in result.get('query',{}).get('search',[]) if re.fullmatch(r'Q\d+',r.get('title',''))]
    def entities(self,ids):
        ids=list(dict.fromkeys(ids))[:45]
        if not ids:return {}
        missing=[q for q in ids if q not in self.entity_cache]
        if missing:self.entity_cache.update(self.wiki(action='wbgetentities',ids='|'.join(missing),props='claims|labels|descriptions|aliases|sitelinks',languages='zh-hans|zh-cn|zh|en|ja',languagefallback=1).get('entities',{}))
        return {q:self.entity_cache[q] for q in ids if q in self.entity_cache}


def values(entity,property):
    return [v for claim in entity.get('claims',{}).get(property,[]) if claim.get('rank')!='deprecated' and (v:=(claim.get('mainsnak',{}).get('datavalue',{}).get('value'))) is not None]


def label(entity):
    for lang in ['zh-hans','zh-cn','zh','en','ja']:
        if value:=entity.get('labels',{}).get(lang,{}).get('value'):return value
    return ''


def commons_image(filename):
    import hashlib
    file=str(filename).replace(' ','_');digest=hashlib.md5(file.encode()).hexdigest();quoted=urllib.parse.quote(file,safe='')
    return f'https://upload.wikimedia.org/wikipedia/commons/thumb/{digest[0]}/{digest[:2]}/{quoted}/500px-{quoted}'+('.png' if file.lower().endswith('.svg') else '')


def search_actors(query,client=None):
    if not query:return []
    c=client or PublicClient();results=[]
    if core.setting_value('tmdb_token'):
        try:
            data=c.tmdb('search/person',query=query,include_adult='false')
            for item in data.get('results',[])[:8]:
                if item.get('known_for_department') not in ('Acting',None):continue
                results.append({'name':item.get('name',''),'source':'tmdb','source_id':str(item['id']),'description':'、'.join((w.get('title') or w.get('name','')) for w in item.get('known_for',[])[:3]),'avatar':'https://image.tmdb.org/t/p/w342'+item['profile_path'] if item.get('profile_path') else ''})
        except (OSError,ValueError):pass
    languages=['zh','ja','en'] if re.search(r'[\u3400-\u9fff\u3040-\u30ff]',query) else ['en','ja','zh']
    for language in languages:
        try:
            data=c.wiki(action='wbsearchentities',search=query,language=language,uselang='zh',limit=10)
            matches=[]
            for item in data.get('search',[]):
                description=str(item.get('description') or '')
                if not any(w in description.casefold() for w in ('actor','actress','演员','演員','俳優','声优','聲優','声優','performer')):continue
                matches.append({'name':item.get('label',query),'aliases':[item.get('match',{}).get('text','')],'source':'wikidata','source_id':item['id'],'description':description,'avatar':''})
            results.extend(matches)
            if matches:break
        except (OSError,ValueError):continue
    from extra_sources import search_extra
    results.extend(search_extra(query,c))
    return list({(p['source'],p['source_id']):p for p in results if p.get('name')}.values())


def tmdb_actor(c,person_id):
    from discovery import GENRES,review_keywords
    p=c.tmdb('person/'+str(person_id),append_to_response='combined_credits,images,translations')
    works=[]
    for w in (p.get('combined_credits') or {}).get('cast',[]):
        if w.get('adult'):continue
        title=w.get('title') or w.get('name')
        if not title:continue
        kind=w.get('media_type','movie');date=w.get('release_date') or w.get('first_air_date') or ''
        works.append(dict(title=title,year=date[:4],genres=[GENRES[g] for g in w.get('genre_ids',[]) if g in GENRES],genre_ids=w.get('genre_ids',[]),poster='https://image.tmdb.org/t/p/w342'+w['poster_path'] if w.get('poster_path') else '',vote_average=w.get('vote_average',0),vote_count=w.get('vote_count',0),popularity=w.get('popularity',0),source='TMDb',url=f"https://www.themoviedb.org/{kind}/{w['id']}",id=str(w['id']),media_type=kind,role=w.get('character','')))
    unique={(w['media_type'],w['id']):w for w in works};works=sorted(unique.values(),key=lambda w:(int(w['vote_count'])>=50, float(w['popularity']),int(w['vote_count'])),reverse=True)[:80]
    images=[{'photo_url':'https://image.tmdb.org/t/p/w500'+i['file_path'],'source':'tmdb','source_id':str(person_id)} for i in (p.get('images') or {}).get('profiles',[])[:30] if i.get('file_path')]
    # Chinese biography is often missing; fall back to verified translations instead of showing nothing.
    biography=p.get('biography','')
    if not biography:
        translations=(p.get('translations') or {}).get('translations',[])
        for lang in ('en-US','en','ja','zh-CN','zh-TW'):
            for t in translations:
                if t.get('iso_639_1')==lang and t.get('data',{}).get('biography'):
                    biography=t['data']['biography'];break
            if biography:break
    departments={'Acting':'表演','Directing':'导演','Production':'制片','Writing':'编剧','Art':'美术','Sound':'音乐','Camera':'摄影','Crew':'剧组','Editing':'剪辑','Costume & Make-Up':'服装化妆'}
    info={k:v for k,v in {'生日':p.get('birthday'),'逝世':p.get('deathday'),'出生地':p.get('place_of_birth'),'职业领域':departments.get(p.get('known_for_department') or '',p.get('known_for_department') or ''),'IMDb':p.get('imdb_id')}.items() if v}
    # Aggregate audience reception from verified ratings and public review samples only.
    rated=[w for w in works if (w.get('vote_count') or 0)>=50 and float(w.get('vote_average') or 0)>0]
    acclaim={}
    if rated:
        votes=sum(int(w['vote_count']) for w in rated)
        average=sum(float(w['vote_average'])*int(w['vote_count']) for w in rated)/max(1,votes)
        top=max(rated,key=lambda w:(float(w['vote_average']),int(w['vote_count'])))
        acclaim=dict(rated_count=len(rated),high_rated=sum(1 for w in rated if float(w['vote_average'])>=8),avg_rating=round(average,1),vote_total=votes,top_work=dict(title=top['title'],score=float(top['vote_average'])))
        performance=[];hit=0;sample=0;sources=[]
        for w in sorted(works,key=lambda x:int(x.get('vote_count') or 0),reverse=True)[:2]:
            if int(w.get('vote_count') or 0)<100:continue
            kind='tv' if w.get('media_type')=='tv' else 'movie'
            try:reviews=c.tmdb(f"{kind}/{w['id']}/reviews",language='en-US').get('results',[])
            except (OSError,ValueError):continue
            sample+=len(reviews)
            labels=[l['label'] for l in review_keywords(reviews) if l['label'] not in performance]
            if labels:hit+=1;performance.extend(labels)
            if reviews:sources.append({'label':'TMDb · 公开评论','url':f"https://www.themoviedb.org/{kind}/{w['id']}/reviews"})
        if performance:
            acclaim['performance_labels']=performance
            acclaim['performance_evidence']=f'已抽查 {sample} 条公开评论样本，{hit} 部代表作明确出现好评；仅代表已获取样本'
            acclaim['performance_sources']=sources
    return {'name':p.get('name',''),'source':'tmdb','source_id':str(person_id),'aliases':p.get('also_known_as',[]),'biography':biography,'avatar':'https://image.tmdb.org/t/p/w500'+p['profile_path'] if p.get('profile_path') else '', 'works':works,'photos':images,'info':info,'acclaim':acclaim,'sources':[{'label':'TMDb · 人物与作品','url':f'https://www.themoviedb.org/person/{person_id}'}]}


def wikidata_actor(c,entity_id):
    from discovery import keywords
    entity=c.entities([entity_id]).get(entity_id,{})
    if not entity or 'Q5' not in [v.get('id') for v in values(entity,'P31') if isinstance(v,dict)]:raise ValueError('公开条目不是人物，未添加。')
    works_ids=[v['id'] for v in values(entity,'P800') if isinstance(v,dict) and v.get('id')][:24]
    if len(works_ids)<4:
        try:works_ids=list(dict.fromkeys(works_ids+c.search_claims('haswbstatement:P161='+entity_id,30)))
        except (OSError,ValueError):pass
    works_data=c.entities(works_ids)
    genre_ids=list(dict.fromkeys(v['id'] for w in works_data.values() for v in values(w,'P136') if isinstance(v,dict) and v.get('id')))
    occupations=[v['id'] for v in values(entity,'P106') if isinstance(v,dict) and v.get('id')]
    award_ids=[v['id'] for v in values(entity,'P166') if isinstance(v,dict) and v.get('id')][:8]
    labels=c.entities(genre_ids+occupations+award_ids)
    award_names=[]
    for aid in award_ids:
        name=label(labels.get(aid,{}))
        if name and name not in award_names:award_names.append(name)
    works=[]
    ordered=sorted(works_data.items(),key=lambda pair:(pair[0] in [v.get('id') for v in values(entity,'P800') if isinstance(v,dict)],len(pair[1].get('sitelinks',{}))),reverse=True)
    for wid,w in ordered:
        if not label(w):continue
        genre_names=[label(labels.get(v.get('id'),{})) for v in values(w,'P136') if isinstance(v,dict)]
        if any(any(term in g.casefold() for term in ('pornograph','色情','成人视频','成人影片')) for g in genre_names):continue
        images=values(w,'P18');dates=values(w,'P577')
        works.append({'title':label(w),'genres':[label(labels.get(v['id'],{})) for v in values(w,'P136') if isinstance(v,dict) and v.get('id') in labels],'genre_ids':[v['id'] for v in values(w,'P136') if isinstance(v,dict) and v.get('id')],'poster':commons_image(images[0]) if images else '','year':str(dates[0].get('time',''))[1:5] if dates else '', 'source':'Wikidata · 演员作品','url':'https://www.wikidata.org/wiki/'+wid,'id':wid,'media_type':'work'})
    images=values(entity,'P18');description=''
    for lang in ['zh-hans','zh-cn','zh','en','ja']:
        if description:=entity.get('descriptions',{}).get(lang,{}).get('value',''):break
    sources=[{'label':'Wikidata · 人物与代表作','url':'https://www.wikidata.org/wiki/'+entity_id}]
    bio=description;warnings=[]
    for lang in ['zh','en','ja']:
        site=entity.get('sitelinks',{}).get(lang+'wiki')
        if not site:continue
        try:
            data=c.get(f'https://{lang}.wikipedia.org/w/api.php?'+urllib.parse.urlencode({'action':'query','format':'json','formatversion':2,'prop':'extracts|pageimages','titles':site['title'],'exintro':1,'explaintext':1,'exchars':1800,'piprop':'thumbnail','pithumbsize':600,'redirects':1}))
            pages=data.get('query',{}).get('pages',[])
            if pages and pages[0].get('extract'):
                bio=pages[0]['extract'];sources.append({'label':f'Wikipedia · {lang}','url':f'https://{lang}.wikipedia.org/wiki/'+urllib.parse.quote(site['title'])})
                if not images and pages[0].get('thumbnail',{}).get('source'):images=[pages[0]['thumbnail']['source']]
                break
        except (OSError,ValueError):warnings.append('部分百科介绍暂未取得')
    jobs=[label(labels.get(q,{})) for q in occupations]
    imdb=next((str(v) for v in values(entity,'P345') if isinstance(v,str) and re.fullmatch(r'nm\d+',v)), '')
    job_tags=[{'label':j,'kind':'职业','sources':sources[:1],'evidence':'Wikidata 职业字段'} for j in jobs if j in {'演员','演員','电影演员','電視演員','声优','配音演员','舞台演员','actor','film actor','television actor','歌手','导演','導演'}]
    avatar=images[0] if images and str(images[0]).startswith('https://') else commons_image(images[0]) if images else ''
    aliases=core.unique_actor_names([*[x.get('value','') for x in entity.get('labels',{}).values()],*[a['value'] for lang in ['zh','zh-hans','en','ja'] for a in entity.get('aliases',{}).get(lang,[])[:12]]])
    genre_ids=list(dict.fromkeys(g for w in works for g in w['genre_ids']))
    return {'name':label(entity),'source':'wikidata','source_id':entity_id,'aliases':aliases,'avatar':avatar,'biography':bio,'works':works,'keywords':keywords(bio,refs=sources)+job_tags,'sources':sources,'warnings':warnings,'wikidata_genres':genre_ids,'awards':award_names[:6],'info':{**({'生日':str(values(entity,'P569')[0].get('time',''))[1:11]} if values(entity,'P569') else {}),**({'IMDb':imdb} if imdb else {})}}


def matching_person(candidate,query_names):
    keys={core.normalize_actor_name(n) for n in query_names if n}
    aliases=[candidate.get('name',''),candidate.get('display_name',''),*candidate.get('aliases',[])]
    return bool(keys.intersection(core.normalize_actor_name(n) for n in aliases if n))


def same_person(a,b):
    ai=a.get('info',{});bi=b.get('info',{})
    if ai.get('IMDb') and ai['IMDb']==bi.get('IMDb'):return True
    def birthday(info):
        numbers=re.findall(r'\d+',str(info.get('生日') or info.get('生年月日') or ''))[:3]
        return tuple(map(int,numbers)) if len(numbers)==3 else None
    birth=birthday(ai)
    if birth and birth==birthday(bi):return True
    return bool({core.normalize_match_text(w.get('title','')) for w in a.get('works',[]) if w.get('title')} & {core.normalize_match_text(w.get('title','')) for w in b.get('works',[]) if w.get('title')})


def fetch_actor(candidate,client=None):
    from extra_sources import bangumi_actor,mal_actor,tvmaze_actor
    c=client or PublicClient();source=candidate.get('source');sid=str(candidate.get('source_id') or '');warnings=[];parts=[]
    loaders={'tmdb':tmdb_actor,'wikidata':wikidata_actor,'bangumi-person':bangumi_actor,'mal-person':mal_actor,'tvmaze-person':tvmaze_actor}
    cached=candidate.get('insights') or {}
    if source not in loaders and cached.get('source') in loaders and cached.get('source_id'):
        source=cached['source'];sid=str(cached['source_id'])
    names=core.unique_actor_names([candidate.get('name',''),candidate.get('display_name',''),*candidate.get('aliases',[])])[:3]
    if source in loaders and sid:
        try:parts.append(loaders[source](c,sid))
        except (OSError,ValueError):warnings.append('已关联来源的部分资料暂未取得')
    # Try stored display names and aliases, across different scripts, before giving up.
    if not parts:
        for name in names:
            matches=search_actors(name,c)
            exact=[m for m in matches if matching_person(m,names)]
            for provider in loaders:
                options=[m for m in exact if m['source']==provider]
                if len(options)!=1:continue
                try:parts.append(loaders[provider](c,options[0]['source_id']))
                except (OSError,ValueError):continue
                if parts:break
            if parts:break
    # IMDb IDs bridge Wikidata and TMDb without guessing a romanization or namesake.
    if parts and core.setting_value('tmdb_token') and not any(p.get('source')=='tmdb' for p in parts):
        imdb=parts[0].get('info',{}).get('IMDb','')
        if re.fullmatch(r'nm\d+',str(imdb)):
            try:
                linked=c.tmdb('find/'+imdb,external_source='imdb_id').get('person_results',[])
                if len(linked)==1:parts.append(tmdb_actor(c,str(linked[0]['id'])))
            except (OSError,ValueError):warnings.append('IMDb 标识关联的部分作品暂未取得')
    if parts and parts[0].get('source')=='tmdb':
        imdb=parts[0].get('info',{}).get('IMDb','')
        if re.fullmatch(r'nm\d+',str(imdb)):
            try:
                ids=c.search_claims('haswbstatement:P345='+imdb,3)
                if len(ids)==1:
                    wiki=wikidata_actor(c,ids[0])
                    if same_person(parts[0],wiki):parts.append(wiki)
            except (OSError,ValueError):warnings.append('部分百科交叉资料暂未取得')
    if parts and len(parts[0].get('works',[]))<4:
        from extra_sources import search_extra
        expanded=core.unique_actor_names([*names,parts[0].get('name',''),*parts[0].get('aliases',[])])
        for query in expanded[:3]:
            try:matches=search_extra(query,c)
            except (OSError,ValueError):continue
            for provider in ['bangumi-person','mal-person','tvmaze-person']:
                options=[m for m in matches if m['source']==provider and matching_person(m,expanded)]
                if len(options)!=1 or any(p.get('source')==provider for p in parts):continue
                try:
                    additional=loaders[provider](c,options[0]['source_id'])
                    if same_person(parts[0],additional):parts.append(additional)
                except (OSError,ValueError):continue
            if any(p.get('works') for p in parts):break
    if not parts:raise ValueError('多来源及已知别名未找到可确认的公开作品；资料库内作品、照片和收藏均保留。可搜索完整姓名后选择具体人物。')
    result=dict(parts[0]);result.update(sources=[],warnings=[],keywords=[],works=[],photos=[],acclaim={},awards=[])
    for part in parts:
        result['sources'].extend(part.get('sources',[]));result['keywords'].extend(part.get('keywords',[]));result['works'].extend(part.get('works',[]));result['photos'].extend(part.get('photos',[]));result['warnings'].extend(part.get('warnings',[]))
        if part.get('biography') and len(part['biography'])>len(result.get('biography','')):result['biography']=part['biography']
        if not result.get('avatar'):result['avatar']=part.get('avatar','')
        if part.get('acclaim') and not result['acclaim']:result['acclaim']=part['acclaim']
        result['awards'].extend(str(a) for a in part.get('awards',[]) if str(a) not in result['awards'])
        if part.get('source')=='wikidata':
            result['wikidata_id']=part['source_id'];result['wikidata_genres']=part.get('wikidata_genres',[])
        if part.get('source')=='tmdb':result['tmdb_id']=part['source_id']
    result['awards']=result['awards'][:8]
    result['fetched_at']=core.now_iso();return result


def similar_candidates(profile):
    c=PublicClient();data=profile.get('insights',{});pool={};warnings=[]
    if data.get('tmdb_id') and core.setting_value('tmdb_token'):
        genres=collections.Counter(g for w in data.get('works',[]) for g in w.get('genre_ids',[]) if isinstance(g,int))
        movie_ids=[]
        for genre,n in genres.most_common(2):
            try:movie_ids.extend(w['id'] for w in c.tmdb('discover/movie',with_genres=str(genre),sort_by='popularity.desc',include_adult='false',**{'vote_count.gte':100}).get('results',[])[:4])
            except (OSError,ValueError):warnings.append('部分同类影片暂未取得')
        for mid in list(dict.fromkeys(movie_ids))[:8]:
            try:
                for p in c.tmdb(f'movie/{mid}/credits').get('cast',[])[:6]:
                    if str(p['id'])==str(data['tmdb_id']) or p.get('adult'):continue
                    pool.setdefault(('tmdb',p['id']),dict(name=p['name'],source='tmdb',source_id=str(p['id']),avatar='https://image.tmdb.org/t/p/w342'+p['profile_path'] if p.get('profile_path') else ''))
            except (OSError,ValueError):warnings.append('部分候选演员未能取得')
    elif data.get('wikidata_genres'):
        counts=collections.Counter(q for w in data.get('works',[]) for q in w.get('genre_ids',[]) if isinstance(q,str) and re.fullmatch(r'Q\d+',q))
        ids=[q for q,n in counts.most_common(3)] or [q for q in data['wikidata_genres'] if re.fullmatch(r'Q\d+',q)][:3]
        own=data.get('wikidata_id') or profile.get('source_id','')
        own_works=[w['id'] for w in data.get('works',[]) if re.fullmatch(r'Q\d+',str(w.get('id','')))][:18]
        movies=list(own_works)
        for gid in ids:
            try:movies.extend(c.search_claims('haswbstatement:P136='+gid+' haswbstatement:P161',6))
            except (OSError,ValueError):warnings.append('部分同类作品暂未取得')
        film_data={};movie_ids=list(dict.fromkeys(movies))[:36]
        for offset in range(0,len(movie_ids),10):
            try:film_data.update(c.entities(movie_ids[offset:offset+10]))
            except (OSError,ValueError):warnings.append('部分作品演职员资料暂未取得')
        actors=collections.Counter()
        for fid,film in film_data.items():
            for v in values(film,'P161')[:8]:
                if isinstance(v,dict) and v.get('id') and v['id']!=own:actors[v['id']]+=3 if fid in own_works else 1
        people={};actor_ids=[aid for aid,n in actors.most_common(16)]
        for offset in range(0,len(actor_ids),6):
            try:people.update(c.entities(actor_ids[offset:offset+6]))
            except (OSError,ValueError):warnings.append('部分候选演员资料暂未取得')
        for sid in actor_ids:
            p=people.get(sid,{})
            if label(p) and 'Q5' in [v.get('id') for v in values(p,'P31') if isinstance(v,dict)]:
                pool[('wikidata',sid)]={'name':label(p),'source':'wikidata','source_id':sid,'avatar':''}
    else:warnings.append('公开作品的类型资料不足；当前仅按资料库中已有标签匹配。配置 TMDb 可扩大公开作品覆盖范围。')
    return list(pool.values())[:10],warnings


def fetch_movie(movie):
    from discovery import keywords,review_keywords
    c=PublicClient();source=movie.get('source');sid=str(movie.get('source_id') or '')
    if source!='tmdb' or not sid.isdigit():raise ValueError('此影片尚未关联 TMDb 条目，请先使用“联网匹配”确认作品，再更新公开关键词和评论。现有类型标签会保留。')
    kind=movie.get('media_type') or 'movie';kind='tv' if kind=='tv' else 'movie'
    d=c.tmdb(f'{kind}/{sid}',append_to_response='keywords,reviews')
    kw=d.get('keywords') or {};kw=kw.get('keywords') or kw.get('results') or []
    refs=[{'label':'TMDb · 作品资料','url':f'https://www.themoviedb.org/{kind}/{sid}'}]
    tags=keywords(d.get('overview','')+' '+' '.join(k.get('name','') for k in kw),[g['name'] for g in d.get('genres',[])],refs)
    reviews=list(d.get('reviews',{}).get('results',[]))
    # Reviews are often unavailable in Chinese; fallback fetches actual English samples.
    if len(reviews)<2:
        r=c.tmdb(f'{kind}/{sid}/reviews',language='en-US',page=1);reviews=r.get('results',[])
    seen={r.get('id') for r in reviews}
    if len(reviews)>=20:
        r=c.tmdb(f'{kind}/{sid}/reviews',language='en-US',page=2)
        reviews.extend(x for x in r.get('results',[]) if x.get('id') not in seen)
    tags.extend(review_keywords(reviews))
    # Preserve only derived evidence and public links, never full reviews.
    return dict(keywords=tags[:16],sources=refs+[{'label':'TMDb · 公开评论','url':f'https://www.themoviedb.org/{kind}/{sid}/reviews'}],review_sample_count=len(reviews),fetched_at=core.now_iso())
