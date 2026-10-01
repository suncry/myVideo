"""Verified public portraits and artwork, with bounded requests and source attribution."""
from __future__ import annotations
import copy,html,re,urllib.parse
from public_sources import PublicClient,values
import app as core

GALLERY_VERSION=1

def plain(value):return html.unescape(re.sub(r'<[^>]+>','',str(value or ''))).strip()

def public_image_url(url):
    parts=urllib.parse.urlsplit(str(url))
    if parts.hostname not in ('upload.wikimedia.org','thumb.wikimedia.org'):return str(url)
    query=urllib.parse.urlencode([(k,v) for k,v in urllib.parse.parse_qsl(parts.query) if not k.startswith('utm_')])
    return urllib.parse.urlunsplit((parts.scheme,parts.netloc,parts.path,query,parts.fragment))

def image_identity(url):
    parts=urllib.parse.urlsplit(str(url));file=urllib.parse.unquote(parts.path.rsplit('/',1)[-1])
    if parts.hostname=='image.tmdb.org':return 'tmdb:'+file
    if parts.hostname in ('upload.wikimedia.org','thumb.wikimedia.org'):return 'wikimedia:'+re.sub(r'^\d+px-','',file)
    return str(url)

def merge_gallery(previous,current):
    """Metadata refresh must retain verified artwork for the same public identity."""
    if not previous.get('gallery_version') or (previous.get('source'),str(previous.get('source_id'))) != (current.get('source'),str(current.get('source_id'))):return current
    data=copy.deepcopy(current)
    data['photos']=unique_images([*current.get('photos',[]),*previous.get('photos',[])])
    lookup={(w.get('media_type'),str(w.get('id'))):w for w in previous.get('works',[])}
    for w in data.get('works',[]):
        old=lookup.get((w.get('media_type'),str(w.get('id'))),{})
        if not w.get('poster') and old.get('poster'):w['poster']=old['poster'];w['poster_source']=old.get('poster_source','')
        w['images']=unique_images([*w.get('images',[]),*old.get('images',[])],20)
    for key in ['gallery_version','media_checked_at','media_warnings']:
        if key in previous:data[key]=previous[key]
    return data

def unique_images(items,limit=50):
    seen=set();out=[]
    for item in items:
        url=public_image_url(item.get('photo_url') or item.get('url') or '')
        if not str(url).startswith(('https://','http://')):continue
        identity=image_identity(url)
        if identity in seen:continue
        seen.add(identity);out.append({**item,'photo_url':url})
    return out[:limit]

def commons_photos(client,entity,actor_id):
    category=next((str(v) for v in values(entity,'P373') if isinstance(v,str)), '')
    linked=entity.get('sitelinks',{}).get('commonswiki',{}).get('title','')
    if not category and linked.startswith('Category:'):category=linked[9:]
    if not category:return [],[]
    categories=['Category:'+category];photos=[];warnings=[]
    base='https://commons.wikimedia.org/w/api.php?'
    try:
        data=client.get(base+urllib.parse.urlencode(dict(action='query',format='json',formatversion=2,list='categorymembers',cmtitle=categories[0],cmtype='subcat',cmlimit=6)))
        categories.extend(p['title'] for p in data.get('query',{}).get('categorymembers',[]) if p.get('title','').startswith('Category:'))
    except (OSError,ValueError):warnings.append('部分人物图集分类暂未取得')
    for category in categories[:5]:
        try:
            data=client.get(base+urllib.parse.urlencode(dict(action='query',format='json',formatversion=2,generator='categorymembers',gcmtitle=category,gcmtype='file',gcmnamespace=6,gcmlimit=50,prop='imageinfo',iiprop='url|mime|size|extmetadata',iiurlwidth=960)))
            for page in data.get('query',{}).get('pages',[]):
                for info in page.get('imageinfo',[])[:1]:
                    if info.get('mime') not in ('image/jpeg','image/png','image/webp'):continue
                    if min(info.get('width',0),info.get('height',0))<180:continue
                    meta=info.get('extmetadata',{})
                    def m(key):return plain(meta.get(key,{}).get('value',''))
                    context=(page.get('title','')+' '+m('ImageDescription')+' '+m('Categories')).casefold()
                    if any(t in context for t in ('handprint','hands.jpg','avenue of stars','boeing','airlines','手印','签名','飞机','nude','naked','pornograph','erotic','裸體','裸体','色情')):continue
                    url=public_image_url(info.get('thumburl') or info.get('url') or '')
                    photos.append(dict(photo_url=url,image_id='commons:'+page['title'],caption=(m('ImageDescription') or page['title'].removeprefix('File:'))[:300],source='Wikimedia Commons',source_id=actor_id,source_url=info.get('descriptionurl',''),credit=m('Artist')[:250],license=m('LicenseShortName')))
            photos=unique_images(photos)
            if len(photos)>=40:break
        except (OSError,ValueError) as exc:
            warnings.append('部分公开人物图片暂未取得')
            if getattr(exc,'code',None)==429:break
    return photos,warnings

def wikipedia_covers(client,works,entities):
    """Follow exact linked article identities; never guess by a shared film title."""
    warnings=[];result=copy.deepcopy(works)
    for lang in ['en','zh','ja']:
        pending={}
        for index,w in enumerate(result[:16]):
            if w.get('poster'):continue
            title=entities.get(str(w.get('id','')),{}).get('sitelinks',{}).get(lang+'wiki',{}).get('title')
            if title:pending[title]=(index,str(w['id']))
        if not pending:continue
        try:
            data=client.get(f'https://{lang}.wikipedia.org/w/api.php?'+urllib.parse.urlencode(dict(action='query',format='json',formatversion=2,prop='pageimages|pageprops',titles='|'.join(pending),piprop='thumbnail|name',pilicense='any',pithumbsize=500,pilimit=20,redirects=1)))
            aliases={x['from']:x['to'] for field in ['normalized','redirects'] for x in data.get('query',{}).get(field,[])}
            for title,(index,wid) in pending.items():
                canonical=title
                for _ in range(4):canonical=aliases.get(canonical,canonical)
                page=next((p for p in data.get('query',{}).get('pages',[]) if p.get('title')==canonical),{})
                actual_id=page.get('pageprops',{}).get('wikibase_item')
                if actual_id and actual_id!=wid:continue
                thumbnail=page.get('thumbnail',{});url=public_image_url(thumbnail.get('source') or '')
                if not url:continue
                image=dict(photo_url=url,caption=result[index].get('title','')+' · 作品条目图片',source='Wikipedia',source_url=f'https://{lang}.wikipedia.org/wiki/'+urllib.parse.quote(canonical),license='详见作品条目及图片来源')
                result[index]['images']=unique_images([*result[index].get('images',[]),image],20)
                if thumbnail.get('height',0)>=thumbnail.get('width',0)*1.1:
                    result[index].update(poster=url,poster_source=image['source_url'])
        except (OSError,ValueError) as exc:
            warnings.append('部分作品封面暂未取得')
            if getattr(exc,'code',None)==429:break
    return result,warnings

def tmdb_artwork(client,works):
    result=copy.deepcopy(works);warnings=[]
    for w in result[:10]:
        kind=w.get('media_type');sid=str(w.get('id',''))
        if kind not in ('movie','tv') or not sid.isdigit():continue
        try:
            data=client.tmdb(f'{kind}/{sid}/images',include_image_language='zh,en,null')
            images=[]
            for group,label in [('posters','海报'),('backdrops','剧照')]:
                for item in data.get(group,[])[:6]:
                    if path:=item.get('file_path'):images.append(dict(photo_url='https://image.tmdb.org/t/p/w1280'+path,caption=w['title']+' · '+label,source='TMDb',source_url=f'https://www.themoviedb.org/{kind}/{sid}/images/{group}',image_id='tmdb:'+path))
            w['images']=unique_images([*w.get('images',[]),*images],16)
            if not w.get('poster') and data.get('posters'):w['poster']='https://image.tmdb.org/t/p/w500'+data['posters'][0]['file_path']
        except (OSError,ValueError) as exc:
            warnings.append('部分作品海报与剧照暂未取得')
            if getattr(exc,'code',None)==429:break
    return result,warnings

def fetch_gallery(profile,client=None):
    c=client or PublicClient();data=copy.deepcopy(profile.get('insights') or profile);photos=list(data.get('photos',[]));warnings=[];works=data.get('works',[])
    wiki_id=data.get('wikidata_id') or (data.get('source_id') if data.get('source')=='wikidata' else '')
    tmdb_id=data.get('tmdb_id') or (data.get('source_id') if data.get('source')=='tmdb' else '')
    if wiki_id and re.fullmatch(r'Q\d+',str(wiki_id)):
        try:
            ids=[str(w['id']) for w in works[:16] if re.fullmatch(r'Q\d+',str(w.get('id','')))];entities=c.entities([wiki_id,*ids]);entity=entities.get(wiki_id,{})
            if 'Q5' not in [v.get('id') for v in values(entity,'P31') if isinstance(v,dict)]:raise ValueError('人物身份未通过核对')
            more,notes=commons_photos(c,entity,wiki_id);photos.extend(more);warnings.extend(notes)
            works,notes=wikipedia_covers(c,works,entities);warnings.extend(notes)
        except (OSError,ValueError):warnings.append('部分百科图集或作品图片暂未取得；现有图片已保留')
    if tmdb_id and core.setting_value('tmdb_token'):
        try:
            person=c.tmdb(f'person/{tmdb_id}/images')
            photos.extend(dict(photo_url='https://image.tmdb.org/t/p/w780'+p['file_path'],image_id='tmdb:'+p['file_path'],source='TMDb',source_id=str(tmdb_id),source_url=f'https://www.themoviedb.org/person/{tmdb_id}/images/profiles',caption=profile.get('name','')+' · 人物照片') for p in person.get('profiles',[])[:50] if p.get('file_path'))
            works,notes=tmdb_artwork(c,works);warnings.extend(notes)
        except (OSError,ValueError):warnings.append('部分人物写真或作品图片暂未取得；现有图片已保留')
    for w in works:
        if w.get('poster'):w['images']=unique_images([dict(photo_url=w['poster'],caption=w.get('title','')+' · 封面',source=w.get('source','公开作品资料'),source_url=w.get('poster_source') or w.get('url','')), *w.get('images',[])],20)
    data.update(photos=unique_images(photos),works=works,gallery_version=GALLERY_VERSION,media_checked_at=core.now_iso(),media_warnings=list(dict.fromkeys(warnings)))
    return data
