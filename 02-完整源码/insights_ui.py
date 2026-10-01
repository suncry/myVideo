from __future__ import annotations
import math
import os
from shiboken6 import isValid
from PySide6.QtCore import Qt,QTimer,QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QWidget,QLabel,QPushButton,QLineEdit,QComboBox,QVBoxLayout,QHBoxLayout,QGridLayout,QStackedWidget,QSizePolicy,QMenu,QFrame,QLayout
import app as core
import discovery
from media_ui import ThumbnailRail,ImageViewer
from scrolling import NativeScrollArea,set_background,ScrollSafeComboBox


def open_source(url):
    if str(url).startswith(('https://','http://')):QDesktopServices.openUrl(QUrl(url))


def text_label(text,name='',wrap=False):
    label=QLabel(str(text));label.setTextFormat(Qt.TextFormat.PlainText);label.setWordWrap(wrap)
    if name:label.setObjectName(name)
    return label


class KeywordStrip(QWidget):
    def __init__(self,parent=None,limit=5):
        super().__init__(parent);self.limit=limit;self.items=[]
        self.line=QHBoxLayout(self);self.line.setContentsMargins(0,0,0,0);self.line.setSpacing(6)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,QSizePolicy.Policy.Fixed)
    def set_keywords(self,items):
        self.items=items
        while self.line.count():
            item=self.line.takeAt(0)
            if item.widget():item.widget().deleteLater()
        for item in items[:self.limit]:
            label=text_label(item['label'][:12],'insightTag')
            label.setMinimumWidth(0);label.setMaximumWidth(85);label.setSizePolicy(QSizePolicy.Policy.Preferred,QSizePolicy.Policy.Fixed)
            label.setToolTip(str(item.get('evidence') or '')+'\n'+str(item.get('kind') or ''))
            self.line.addWidget(label)
        if len(items)>self.limit:
            more=QPushButton(f'+{len(items)-self.limit}');more.setObjectName('compactText');more.clicked.connect(self.show_evidence);self.line.addWidget(more)
        self.line.addStretch();self.setVisible(bool(items))
    def show_evidence(self):
        menu=QMenu(self)
        for item in self.items:
            action=menu.addAction(item['label']+' · '+item.get('kind',''));action.setToolTip(item.get('evidence',''));action.setEnabled(False)
            note=menu.addAction(item.get('evidence') or '已有资料');note.setEnabled(False)
            for source in item.get('sources',[]):
                url=source.get('url') if isinstance(source,dict) else source
                if url:
                    action=menu.addAction(source.get('label','查看公开来源') if isinstance(source,dict) else '查看评论来源')
                    action.triggered.connect(lambda checked=False,u=url:open_source(u))
            menu.addSeparator()
        menu.exec(self.mapToGlobal(self.rect().bottomLeft()))


class ActorLibrary(QWidget):
    def __init__(self,owner,poster_class):
        super().__init__(owner);self.owner=owner;self.poster_class=poster_class;self.actor=None;self.tiles=[];self.columns=0;self.busy=False;self.gallery_attempted=set();self.gallery_pending=set();self.viewer=None
        self.layout=QVBoxLayout(self);self.layout.setContentsMargins(0,0,0,0)
        self.stack=QStackedWidget();self.layout.addWidget(self.stack)
        self.browse=QWidget();self.browse_layout=QVBoxLayout(self.browse);self.browse_layout.setContentsMargins(28,27,28,12);self.browse_layout.setSpacing(15)
        header=QHBoxLayout();header.addWidget(text_label('演员','heading'));header.addStretch();self.browse_layout.addLayout(header)
        self.browse_layout.addWidget(text_label('收藏喜欢的演员，沿着作品与标签发现新的名字。','muted'))
        filters=QHBoxLayout();self.query=QLineEdit();self.query.setPlaceholderText('搜索姓名、别名或关键词');self.query.setClearButtonEnabled(True)
        self.scope=ScrollSafeComboBox();self.scope.addItems(['全部演员','我的收藏','资料库内'])
        self.public_search=QPushButton('搜索公开演员');self.public_search.setToolTip('查询 TMDb、百科、TVmaze、Bangumi 和 MyAnimeList；重名条目由你选择');self.public_search.clicked.connect(self.search_public)
        filters.addWidget(self.query,1);filters.addWidget(self.scope);filters.addWidget(self.public_search);self.browse_layout.addLayout(filters)
        self.browse_status=text_label('','mutedSmall',True);self.browse_layout.addWidget(self.browse_status)
        self.grid_scroll=NativeScrollArea();self.grid_scroll.setWidgetResizable(True);self.grid_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        set_background(self.grid_scroll.viewport(),'#101115');self.grid_content=QWidget();set_background(self.grid_content,'#101115');self.grid=QGridLayout(self.grid_content);self.grid.setContentsMargins(0,0,0,0);self.grid.setSpacing(18);self.grid.setAlignment(Qt.AlignmentFlag.AlignTop|Qt.AlignmentFlag.AlignLeft);self.grid_scroll.setWidget(self.grid_content);self.browse_layout.addWidget(self.grid_scroll,1)
        self.stack.addWidget(self.browse)
        self.detail_scroll=NativeScrollArea();self.detail_scroll.setWidgetResizable(True);self.detail_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff);set_background(self.detail_scroll.viewport(),'#101115')
        self.body=QWidget();set_background(self.body,'#101115');self.body_layout=QVBoxLayout(self.body);self.body_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize);self.body_layout.setContentsMargins(30,24,30,32);self.body_layout.setSpacing(14);self.detail_scroll.setWidget(self.body);self.stack.addWidget(self.detail_scroll)
        self.debounce=QTimer(self);self.debounce.setSingleShot(True);self.debounce.timeout.connect(self.reload);self.query.textChanged.connect(lambda:self.debounce.start(160));self.scope.currentIndexChanged.connect(self.reload)
    def reload(self):
        self.busy=False;self.public_search.setEnabled(True)
        profiles=discovery.actor_list(self.query.text(),self.scope.currentIndex()==1,self.scope.currentIndex()==2)
        self.show_tiles(profiles)
        self.browse_status.setText(f'{len(profiles)} 位演员'+(' · 还没有收藏，在演员详情中点击“收藏演员”。' if not profiles and self.scope.currentIndex()==1 else ''))
    def show_tiles(self,profiles,external=False):
        while self.grid.count():
            w=self.grid.takeAt(0).widget()
            if w:w.deleteLater()
        self.tiles=[]
        for p in profiles:
            tile=self.actor_tile(p,external=external);self.tiles.append(tile)
        self.columns=0;self.reflow()
    def actor_tile(self,p,external=False,recommendation=False):
        tile=QWidget();tile.setFixedWidth(154);box=QVBoxLayout(tile);box.setContentsMargins(0,0,0,0);box.setSpacing(6)
        image=self.poster_class();image.setFixedSize(154,194);image.setCursor(Qt.CursorShape.PointingHandCursor);image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.owner.images.load(p.get('avatar') or p.get('avatar_url',''),image,image.size(),p.get('display_name') or p['name'],True,True,True)
        callback=(lambda:self.add_candidate(p)) if external else (lambda:self.show_actor(p['name']))
        image.clicked.connect(callback);box.addWidget(image)
        name=QPushButton(p.get('display_name') or p['name']);name.setObjectName('actorLibraryName');name.setToolTip(p['name']);name.clicked.connect(callback);box.addWidget(name)
        if external:
            desc=text_label(str(p.get('description') or p.get('source') or '')[:70],'mutedSmall',True);desc.setFixedHeight(42);box.addWidget(desc)
            button=QPushButton('添加并查看');button.clicked.connect(callback)
        else:
            if recommendation:
                status='资料库尚无作品' if not p.get('local_works') else f"资料库已有 {len(p['local_works'])} 部"
                desc=text_label(' · '.join([*p.get('shared_keywords',[])[:2],status]),'actorReason',True);desc.setFixedHeight(40);box.addWidget(desc)
            else:box.addWidget(text_label(f"{len(p.get('local_works',[]))} 部资料库作品",'mutedSmall'))
            button=QPushButton('已收藏' if p.get('favorite') else '收藏演员');button.setCheckable(True);button.setChecked(bool(p.get('favorite')))
            button.clicked.connect(lambda checked:self.toggle_favorite(p['name'],checked,button))
        button.setObjectName('actorSave');box.addWidget(button);box.addStretch();return tile
    def reflow(self):
        cols=max(1,(self.grid_scroll.viewport().width()+18)//172)
        if cols==self.columns:return
        while self.grid.count():self.grid.takeAt(0)
        self.columns=cols
        for i,tile in enumerate(self.tiles):self.grid.addWidget(tile,i//cols,i%cols)
        self.grid_content.setMinimumHeight(math.ceil(len(self.tiles)/cols)*305)
    def resizeEvent(self,event):super().resizeEvent(event);QTimer.singleShot(0,self.reflow)
    def toggle_favorite(self,name,value,button):
        discovery.set_actor_favorite(name,value);button.setText('已收藏' if value else '收藏演员');button.setChecked(value)
        if self.actor and self.actor['name']==name:self.actor['favorite']=value
    def clear_detail(self):
        while self.body_layout.count():
            item=self.body_layout.takeAt(0)
            if item.widget():item.widget().deleteLater()
            elif item.layout():
                layout=item.layout()
                while layout.count():
                    child=layout.takeAt(0)
                    if child.widget():child.widget().deleteLater()
                layout.deleteLater()
    def show_actor(self,name):
        p=discovery.actor_profile(name)
        if not p:return
        self.actor=p;self.clear_detail();self.stack.setCurrentIndex(1);self.detail_scroll.verticalScrollBar().setValue(0)
        nav=QHBoxLayout();back=QPushButton('返回演员');back.setObjectName('compactText');back.clicked.connect(self.go_back);nav.addWidget(back);nav.addStretch()
        edit=QPushButton('管理照片');edit.clicked.connect(lambda:self.manage_photos(p['name']));nav.addWidget(edit);self.body_layout.addLayout(nav)
        hero=QWidget();hero_layout=QHBoxLayout(hero);hero_layout.setContentsMargins(0,8,0,8);hero_layout.setSpacing(24)
        portrait=self.poster_class();portrait.setFixedSize(204,264);portrait.setAlignment(Qt.AlignmentFlag.AlignCenter);self.owner.images.load(p['avatar'],portrait,portrait.size(),p.get('display_name') or p['name'],True,True,True);portrait.setCursor(Qt.CursorShape.PointingHandCursor);portrait.setToolTip('点击预览人物图集');portrait.clicked.connect(lambda:self.preview_images(self.actor_photos(p),title=p.get('display_name') or p['name'],actor=True));hero_layout.addWidget(portrait,0,Qt.AlignmentFlag.AlignTop)
        column=QVBoxLayout();column.setSpacing(9);column.addWidget(text_label(p.get('display_name') or p['name'],'actorDetailName',True))
        aliases=' · '.join([p['name'],*[n for n in p['aliases'] if n not in [p['name'],p.get('display_name')]]][:4]);column.addWidget(text_label(aliases,'mutedSmall',True))
        bio=p.get('biography') or '还没有可核对的演员介绍。更新公开资料后，这里会展示介绍和来源。'
        self.bio=text_label(bio[:260]+('…' if len(bio)>260 else ''),'actorBiography',True);column.addWidget(self.bio)
        if len(bio)>260:
            expand=QPushButton('展开完整介绍');expand.setObjectName('compactText');expand.clicked.connect(lambda:self.expand_bio(bio,expand));column.addWidget(expand)
        actions=QHBoxLayout();self.save=QPushButton('已收藏' if p['favorite'] else '收藏演员');self.save.setObjectName('actorSave');self.save.setCheckable(True);self.save.setChecked(p['favorite']);self.save.clicked.connect(lambda value:self.toggle_favorite(p['name'],value,self.save));actions.addWidget(self.save)
        search=QPushButton('网页搜索');search.clicked.connect(lambda:self.owner.search_actor(p.get('display_name') or p['name']));actions.addWidget(search);actions.addStretch();column.addLayout(actions);column.addStretch();hero_layout.addLayout(column,1);self.body_layout.addWidget(hero)
        if p['info']:self.body_layout.addWidget(text_label('  ·  '.join(f'{k}：{v}' for k,v in list(p['info'].items())[:7]),'mutedSmall',True))
        if p.get('awards'):self.body_layout.addWidget(text_label('获奖记录：'+'、'.join(p['awards'][:6])+(f" 等 {len(p['awards'])} 项" if len(p['awards'])>6 else ''),'mutedSmall',True))
        acclaim=p.get('acclaim') or {}
        reception=[]
        if acclaim.get('rated_count'):reception.append(f"{acclaim['rated_count']} 部公开代表作有评分")
        if acclaim.get('avg_rating'):reception.append(f"按评分人数加权均分 {acclaim['avg_rating']}")
        if acclaim.get('high_rated'):reception.append(f"{acclaim['high_rated']} 部达 8 分以上")
        if acclaim.get('top_work'):reception.append(f"最高分《{acclaim['top_work']['title']}》{float(acclaim['top_work']['score']):.1f}")
        if acclaim.get('performance_labels'):reception.append('公开评论样本中其表演获得明确好评：'+'、'.join(acclaim['performance_labels']))
        if reception:
            head=QHBoxLayout();head.addWidget(text_label('大众评价','actorSection'));head.addWidget(text_label('仅汇总已核实的公开评分与评论样本','mutedSmall'));head.addStretch();self.body_layout.addLayout(head)
            self.body_layout.addWidget(text_label('；'.join(reception)+'。','mutedSmall',True))
        taghead=QHBoxLayout();taghead.addWidget(text_label('关键词','actorSection'));taghead.addStretch();refresh=QPushButton('更新公开资料');refresh.clicked.connect(lambda:self.refresh_public(p['name'],refresh));taghead.addWidget(refresh);self.body_layout.addLayout(taghead)
        tags=KeywordStrip(limit=5);tags.set_keywords(p['keywords']);self.body_layout.addWidget(tags)
        if not p['keywords']:self.body_layout.addWidget(text_label('作品资料不足，暂不生成标签。','mutedSmall'))
        self.detail_status=text_label('资料更新：'+p['fetched_at'].replace('T',' ')[:19] if p['fetched_at'] else '标签来自已收录作品；点击标签旁的更多按钮可查看依据。','mutedSmall',True);self.body_layout.addWidget(self.detail_status)
        if p['warnings']:self.body_layout.addWidget(text_label('；'.join(p['warnings']),'mutedSmall',True))
        sources=list(p['sources'])
        for k,url in core.json_value(p.get('source_refs_json'),{}).items():
            if str(url).startswith(('https://','http://')) and url not in [s.get('url') for s in sources]:sources.append({'label':k,'url':url})
        if sources:
            line=QHBoxLayout();line.addWidget(text_label('公开来源','mutedSmall'))
            for source in sources[:5]:
                b=QPushButton(source['label']);b.setObjectName('compactText');b.setToolTip(source.get('url',''));b.clicked.connect(lambda checked=False,url=source.get('url',''):open_source(url));line.addWidget(b)
            line.addStretch();self.body_layout.addLayout(line)
        workhead=QHBoxLayout();workhead.addWidget(text_label('主要作品','actorSection'))
        hidden=len(p.get('insights',{}).get('works',[]))-len(p['works'])
        if hidden>0:workhead.addWidget(text_label(f"已隐藏与资料库重复的 {hidden} 部，只展示还没有的",'mutedSmall'))
        workhead.addStretch();self.artwork_update=QPushButton('正在补充图片…' if name in self.gallery_pending else '刷新图片与封面');self.artwork_update.setEnabled(name not in self.gallery_pending);self.artwork_update.clicked.connect(lambda:self.refresh_media(name));workhead.addWidget(self.artwork_update);self.body_layout.addLayout(workhead)
        works=p['works'][:16]
        if works:self.body_layout.addWidget(self.work_strip(works))
        elif p['local_works']:
            message='公开代表作均已收入资料库，新的公开作品会出现在这里。' if p['insights'].get('works') else '公开代表作尚未确认，先展示资料库中已有的参演作品。'
            self.body_layout.addWidget(text_label(message,'mutedSmall',True));films=QPushButton('按此演员筛选影片');films.setObjectName('compactText');films.clicked.connect(lambda:self.owner.view_actor_films(p['name']));self.body_layout.addWidget(films);self.body_layout.addWidget(self.work_strip(p['local_works'][:16],local=True))
        else:self.body_layout.addWidget(text_label('暂未取得可确认的公开代表作；资料库中也尚无此演员的作品。','mutedSmall',True))
        photos=self.actor_photos(p)
        if photos:
            photohead=QHBoxLayout();photohead.addWidget(text_label('人物图集','actorSection'));photohead.addWidget(text_label(f'{len(photos)} 张 · 点击查看大图','mutedSmall'));photohead.addStretch();all_photos=QPushButton('浏览图集');all_photos.clicked.connect(lambda:self.preview_images(photos,title=p.get('display_name') or p['name'],actor=True));photohead.addWidget(all_photos);self.body_layout.addLayout(photohead);self.body_layout.addWidget(self.photo_strip(photos))
        else:self.body_layout.addWidget(text_label('暂无可确认的人物图集，可点击“刷新图片与封面”尝试补充。','mutedSmall',True))
        if p['insights'].get('media_warnings'):self.body_layout.addWidget(text_label('；'.join(p['insights']['media_warnings']),'mutedSmall',True))
        if p['local_works'] and works:
            local_head=QHBoxLayout();local_head.addWidget(text_label('我的资料库','actorSection'));local_head.addStretch();films=QPushButton('按此演员筛选影片');films.clicked.connect(lambda:self.owner.view_actor_films(p['name']));local_head.addWidget(films);self.body_layout.addLayout(local_head)
            self.body_layout.addWidget(self.work_strip(p['local_works'][:16],local=True))
        similar_head=QHBoxLayout();similar_head.addWidget(text_label('你也可能喜欢','actorSection'));similar_head.addStretch();discover=QPushButton('联网寻找同类演员');discover.clicked.connect(lambda:self.discover_public(p['name'],discover));similar_head.addWidget(discover);self.body_layout.addLayout(similar_head)
        self.rec_container=QWidget();set_background(self.rec_container,'#101115');self.rec_layout=QHBoxLayout(self.rec_container);self.rec_layout.setContentsMargins(0,0,0,0);self.rec_layout.setSpacing(18)
        self.rec_scroll=NativeScrollArea(horizontal_only=True);self.rec_scroll.setWidgetResizable(False);self.rec_scroll.setFixedHeight(318);set_background(self.rec_scroll.viewport(),'#101115');self.rec_scroll.setWidget(self.rec_container);self.body_layout.addWidget(self.rec_scroll)
        self.show_recommendations(discovery.local_recommendations(p['name']))
        self.body_layout.addStretch()
        self.body_layout.activate()
        self.body.setMinimumHeight(self.body_layout.minimumSize().height())
        known=p.get('source') in ('tmdb','wikidata') or p['insights'].get('tmdb_id') or p['insights'].get('wikidata_id')
        if known and os.environ.get('YINGKU_DISABLE_STARTUP_TASKS')!='1' and not p['insights'].get('gallery_version') and name not in self.gallery_attempted:
            self.gallery_attempted.add(name);QTimer.singleShot(200,lambda:self.refresh_media(name) if self.actor and self.actor['name']==name else None)
    def show_recommendations(self,profiles):
        while self.rec_layout.count():
            item=self.rec_layout.takeAt(0)
            if item.widget():item.widget().deleteLater()
        if profiles:
            for p in profiles[:10]:self.rec_layout.addWidget(self.actor_tile(p,recommendation=True))
            self.rec_container.setFixedSize(len(profiles[:10])*172-18,302)
        else:
            label=text_label('还没有资料足够的同类演员。更新公开资料后可扩大匹配范围。','mutedSmall',True);label.setFixedWidth(460);self.rec_layout.addWidget(label);self.rec_container.setFixedSize(500,70);self.rec_scroll.setFixedHeight(85)
    def expand_bio(self,text,button):
        expanded=button.text().startswith('展开');self.bio.setText(text if expanded else text[:260]+'…');button.setText('收起介绍' if expanded else '展开完整介绍')
    def work_strip(self,works,local=False):
        scroll=ThumbnailRail();scroll.setFixedHeight(290);container=QWidget();set_background(container,'#101115');line=QHBoxLayout(container);line.setContentsMargins(0,0,0,0);line.setSpacing(15)
        for w in works:
            tile=QWidget();tile.setFixedWidth(154);box=QVBoxLayout(tile);box.setContentsMargins(0,0,0,0);box.setSpacing(5)
            image=self.poster_class();image.setFixedSize(154,218);image.setAlignment(Qt.AlignmentFlag.AlignCenter);image.setCursor(Qt.CursorShape.PointingHandCursor);poster=w.get('local_poster') or w.get('poster') or w.get('poster_url','')
            scroll.add_image(self.owner.images,image,poster,w.get('title',''))
            image.clicked.connect((lambda item=w:self.owner.open_actor_movie(item['id'])) if local else (lambda item=w:self.preview_work(item)));box.addWidget(image)
            title=text_label(w.get('title',''),'actorWorkTitle');title.setToolTip(w.get('title',''));box.addWidget(title)
            box.addWidget(text_label(str(w.get('year') or '')+(' · 资料库' if local else ' · '+str(len(w.get('images',[])))+' 张图片'),'mutedSmall'));line.addWidget(tile)
        container.setFixedSize(len(works)*169-15,277);scroll.setWidget(container);return scroll
    def actor_photos(self,p):
        from actor_media import image_identity
        metadata={image_identity(x.get('photo_url','')):x for x in p['insights'].get('photos',[])}
        result=[];seen=set()
        for choice in core.actor_photo_choices(p['name_key']):
            identity=image_identity(choice['photo_url'])
            if identity in seen:continue
            seen.add(identity);meta=metadata.get(identity,{})
            result.append({**choice,**meta,'photo_url':choice['photo_url'],'caption':meta.get('caption') or p.get('display_name') or p['name']})
        return result
    def photo_strip(self,photos):
        scroll=ThumbnailRail();scroll.setFixedHeight(253);container=QWidget();set_background(container,'#101115');line=QHBoxLayout(container);line.setContentsMargins(0,0,0,0);line.setSpacing(12)
        for index,photo in enumerate(photos):
            tile=QWidget();tile.setFixedWidth(164);box=QVBoxLayout(tile);box.setContentsMargins(0,0,0,0);box.setSpacing(5)
            image=self.poster_class();image.setFixedSize(164,212);image.setAlignment(Qt.AlignmentFlag.AlignCenter);image.setCursor(Qt.CursorShape.PointingHandCursor);image.setToolTip('点击预览大图；预览中可设为头像')
            scroll.add_image(self.owner.images,image,photo['photo_url'],self.actor['name'],focus=True);image.clicked.connect(lambda i=index:self.preview_images(photos,i,title=self.actor.get('display_name') or self.actor['name'],actor=True));box.addWidget(image)
            caption=text_label(str(photo.get('caption') or photo.get('source') or '人物图片')[:22],'mutedSmall');caption.setToolTip(photo.get('caption',''));box.addWidget(caption);line.addWidget(tile)
        container.setFixedSize(len(photos)*176-12,240);scroll.setWidget(container);return scroll
    def preview_images(self,items,index=0,title='',actor=False):
        if not items:return
        if self.viewer:self.viewer.close()
        self.viewer=ImageViewer(self.owner,items,index,title=title,set_avatar=self.choose_photo if actor else None);self.viewer.setWindowModality(Qt.WindowModality.WindowModal);self.viewer.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose);self.viewer.finished.connect(lambda:self.viewer_closed());self.viewer.show();self.viewer.raise_()
    def viewer_closed(self):self.viewer=None
    def preview_work(self,work):
        images=list(work.get('images',[]))
        poster=work.get('poster') or work.get('poster_url')
        if poster and not any(x.get('photo_url')==poster for x in images):images.insert(0,dict(photo_url=poster,caption=work['title']+' · 封面',source=work.get('source','公开资料'),source_url=work.get('url','')))
        if images:self.preview_images(images,title=work['title'])
        else:open_source(work.get('url',''))
    def refresh_media(self,name):
        if name in self.gallery_pending:return
        self.gallery_pending.add(name);self.gallery_attempted.add(name)
        if self.actor and self.actor['name']==name:
            self.artwork_update.setEnabled(False);self.artwork_update.setText('正在补充图片…');self.detail_status.setText('正在读取人物图集与主要作品封面…')
        def done(profile):
            self.gallery_pending.discard(name)
            if self.actor and self.actor['name']==name:
                position=self.detail_scroll.verticalScrollBar().value();self.show_actor(name);QTimer.singleShot(0,lambda:self.detail_scroll.verticalScrollBar().setValue(position))
        def failed(error):
            self.gallery_pending.discard(name)
            if self.actor and self.actor['name']==name:self.artwork_update.setEnabled(True);self.artwork_update.setText('刷新图片与封面');self.detail_status.setText(error)
        self.owner.run_task(lambda:discovery.refresh_actor_media(name),done,failed)
    def choose_photo(self,url):
        core.select_actor_photo(self.actor['name_key'],url);self.owner.images.cache.clear();self.owner.load_actor_strip();self.show_actor(self.actor['name'])
    def manage_photos(self,name):self.owner.open_actor_photo_dialog(name);self.show_actor(name)
    def go_back(self):self.stack.setCurrentIndex(0);self.reload()
    def refresh_public(self,name,button):
        button.setEnabled(False);self.detail_status.setText('正在读取公开介绍、照片与作品…')
        self.owner.run_task(lambda:discovery.refresh_actor(name),lambda result:self.updated(name),lambda error:self.detail_error(name,button,error))
    def updated(self,name):
        self.owner.images.cache.clear();self.owner.load_actor_strip()
        if self.actor and self.actor['name']==name:self.show_actor(name)
    def detail_error(self,name,button,message):
        if self.actor and self.actor['name']==name:
            if isValid(button):button.setEnabled(True)
            self.detail_status.setText(message)
    def discover_public(self,name,button):
        button.setEnabled(False);self.detail_status.setText('正在按作品标签寻找并核对同类演员的公开资料…')
        def complete(result):
            if self.actor and self.actor['name']==name:
                if isValid(button):button.setEnabled(True)
                self.rec_scroll.setFixedHeight(318);self.show_recommendations(result['recommendations']);self.detail_status.setText(f"已核对 {result['candidates_checked']} 位公开候选；推荐依据显示在演员卡片下方。"+(' '+ '；'.join(result['warnings']) if result['warnings'] else ''))
        self.owner.run_task(lambda:discovery.discover_similar(name),complete,lambda error:self.detail_error(name,button,error))
    def search_public(self):
        query=self.query.text().strip()
        if not query:self.browse_status.setText('先输入演员姓名，再搜索公开资料。');return
        self.public_search.setEnabled(False);self.browse_status.setText('正在搜索公开人物条目…')
        def done(results):
            self.public_search.setEnabled(True);self.show_tiles(results,external=True);self.browse_status.setText(f'找到 {len(results)} 个公开条目。请根据简介确认姓名相同的人物。' if results else '暂未找到公开条目；本地演员和收藏保留完整。')
        self.owner.run_task(lambda:discovery.search_public_actors(query),done,lambda e:self.search_failed(e))
    def search_failed(self,message):self.public_search.setEnabled(True);self.browse_status.setText(message)
    def add_candidate(self,p):
        if self.busy:return
        self.busy=True;self.browse_status.setText('正在核对公开资料并添加演员…')
        def done(profile):self.busy=False;self.show_actor(profile['name'])
        def failed(message):self.busy=False;self.browse_status.setText(message)
        self.owner.run_task(lambda:discovery.add_public_actor(p),done,failed)
