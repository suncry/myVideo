import unittest
from unittest import mock
import public_sources as public,extra_sources as extra


def person(source='wikidata',sid='Q1',**fields):
    return dict(name='演员',source=source,source_id=sid,works=[],aliases=[],info={},sources=[],**fields)


class ExtraSourceTests(unittest.TestCase):
    def test_one_provider_failure_does_not_discard_other_provider_results(self):
        c=mock.Mock();c.post.side_effect=OSError('offline')
        c.get.side_effect=[OSError('timeout'),[{'person':{'id':7,'name':'演员','image':None}}]]
        self.assertEqual(extra.search_extra('演员',c)[0]['source'],'tvmaze-person')

    def test_null_jikan_names_do_not_prevent_tvmaze_fallback(self):
        c=mock.Mock();c.post.return_value={'data':[]}
        c.get.side_effect=[{'data':[{'mal_id':1,'name':'演员','given_name':None,'family_name':None,'alternate_names':None,'images':None}]},[{'person':{'id':7,'name':'演员'}}]]
        self.assertEqual({p['source'] for p in extra.search_extra('演员',c)},{'mal-person','tvmaze-person'})

    def test_bangumi_uses_voice_characters_and_checks_subject_content(self):
        c=mock.Mock();c.get.side_effect=[{'name':'演员','infobox':[{'key':'别名','value':'别名'}]},[
            {'subject_id':10,'subject_type':2,'staff':'主角'},
            {'subject_id':10,'subject_type':2,'staff':'配角'},
            {'subject_id':11,'subject_type':2,'staff':'主角'},
        ],{'name':'作品','nsfw':False,'images':{'large':'https://example.test/cover.jpg'},'collection':{'collect':100}},
        {'name':'不展示','nsfw':True}]
        p=extra.bangumi_actor(c,'1');self.assertEqual(len(p['works']),1);self.assertIn('/characters',c.get.call_args_list[1].args[0]);self.assertEqual(p['works'][0]['poster'],'https://example.test/cover.jpg');self.assertIn('别名',p['aliases'])

    def test_tvmaze_deduplicates_confirmed_credits_and_retains_real_covers(self):
        show={'id':5,'name':'剧集','genres':['Comedy'],'weight':90,'image':{'original':'https://example.test/cover.jpg'}}
        c=mock.Mock();c.get.side_effect=[{'name':'演员','birthday':'1970-01-02'},[
            {'_embedded':{'show':show}},{'_embedded':{'show':show}},
            {'_embedded':{'show':dict(show,id=6,genres=['Adult'])}}]]
        p=extra.tvmaze_actor(c,'7');self.assertEqual(len(p['works']),1);self.assertEqual(p['info']['生日'],'1970-01-02');self.assertIn('castcredits?embed=show',c.get.call_args.args[0])

    def test_jikan_requires_confirmed_non_explicit_anime_metadata(self):
        c=mock.Mock();c.get.side_effect=[{'data':{'name':'演员','alternate_names':['别名']}},
            {'data':[{'anime':{'mal_id':1}},{'anime':{'mal_id':2}},{'anime':{'mal_id':3}}]},
            {'data':{'title':'作品','rating':'PG-13','images':{'jpg':{'large_image_url':'https://example.test/cover.jpg'}}}},
            {'data':{'title':'无评级'}},{'data':{'title':'不展示','rating':'Rx - Hentai'}}]
        p=extra.mal_actor(c,'7');self.assertEqual([w['title'] for w in p['works']],['作品'])

    def test_stored_alias_is_tried_after_unmatched_english_name(self):
        candidate={'name':'English Name','display_name':'花澤香菜','aliases':['花泽香菜'],'source':'catalog-actor'}
        hit={'name':'花澤香菜','source':'bangumi-person','source_id':'4765'}
        def search(query,c):return [hit] if query=='花澤香菜' else []
        with mock.patch.object(public.core,'setting_value',return_value=''),mock.patch.object(public,'search_actors',side_effect=search) as searches,mock.patch.object(extra,'bangumi_actor',return_value=person('bangumi-person','4765',biography='介绍')) ,mock.patch.object(extra,'search_extra',return_value=[]):
            p=public.fetch_actor(candidate,mock.Mock());self.assertEqual(p['source'],'bangumi-person');self.assertEqual([call.args[0] for call in searches.call_args_list],['English Name','花澤香菜'])

    def test_previously_confirmed_identity_is_reused_without_name_search(self):
        candidate={'name':'旧名','source':'catalog-actor','insights':{'source':'wikidata','source_id':'Q1'}}
        with mock.patch.object(public.core,'setting_value',return_value=''),mock.patch.object(public,'search_actors') as search,mock.patch.object(public,'wikidata_actor',return_value=person(biography='介绍')),mock.patch.object(extra,'search_extra',return_value=[]):
            p=public.fetch_actor(candidate,mock.Mock());search.assert_not_called();self.assertEqual(p['wikidata_id'],'Q1')

    def test_matching_name_alone_does_not_merge_unrelated_sources(self):
        primary=person(biography='介绍');other=person('tvmaze-person','7',biography='错误介绍')
        with mock.patch.object(public.core,'setting_value',return_value=''),mock.patch.object(public,'wikidata_actor',return_value=primary),mock.patch.object(extra,'search_extra',return_value=[{'name':'演员','source':'tvmaze-person','source_id':'7'}]),mock.patch.object(extra,'tvmaze_actor',return_value=other):
            result=public.fetch_actor(primary,mock.Mock());self.assertEqual(result['biography'],'介绍')
        self.assertFalse(public.same_person(primary,other))

    def test_imdb_identity_links_encyclopedia_to_tmdb(self):
        a=person();a['info']={'IMDb':'nm123'}
        b=person('tmdb','5');b['works']=[{'id':'1','title':'电影'}]
        c=mock.Mock();c.tmdb.return_value={'person_results':[{'id':5}]}
        with mock.patch.object(public.core,'setting_value',return_value='token'),mock.patch.object(public,'wikidata_actor',return_value=a),mock.patch.object(public,'tmdb_actor',return_value=b),mock.patch.object(extra,'search_extra',return_value=[]):
            p=public.fetch_actor(a,c);self.assertEqual(p['tmdb_id'],'5');self.assertEqual(p['works'][0]['title'],'电影');c.tmdb.assert_called_once_with('find/nm123',external_source='imdb_id')
