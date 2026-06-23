import hashlib
import itertools

from .common import InfoExtractor
from ..utils import (
    ExtractorError,
    float_or_none,
    int_or_none,
    join_nonempty,
    str_or_none,
    xpath_text,
)
from ..utils.traversal import traverse_obj


class YandexMusicBaseIE(InfoExtractor):
    _VALID_URL_BASE = r'https?://music\.yandex\.(?P<tld>ru|kz|ua|by|com)'
    _API_BASE = 'https://api.music.yandex.net'
    # Salt used to sign the get-mp3 download URL, see _extract_formats
    _MP3_SALT = 'XGRlBW9FXlekgbPrRHuSiA'
    _CLIENT = 'YandexMusicAndroid/24023621'

    def _get_token(self):
        return self._configuration_arg(
            'token', [None], ie_key='YandexMusic', casesense=True)[0]

    def _api_headers(self):
        headers = {'X-Yandex-Music-Client': self._CLIENT}
        token = self._get_token()
        if token:
            headers['Authorization'] = f'OAuth {token}'
        return headers

    def _call_api(self, path, item_id, note='Downloading JSON metadata', query=None,
                  data=None, fatal=True):
        response = self._download_json(
            f'{self._API_BASE}/{path}', item_id, note, fatal=fatal,
            headers=self._api_headers(), query=query, data=data)
        if not response:
            return response
        error = traverse_obj(response, ('error', ('message', 'name'), {str}, any))
        if error:
            raise ExtractorError(f'YandexMusic said: {error}', expected=True)
        return response.get('result')

    def _extract_formats(self, track, track_id):
        formats = []
        download_info = self._call_api(
            f'tracks/{track_id}/download-info', track_id,
            'Downloading track download info', fatal=False)
        only_preview = False
        for info in traverse_obj(download_info, lambda _, v: v['downloadInfoUrl']):
            if info.get('preview'):
                only_preview = True
                continue
            doc = self._download_xml(
                info['downloadInfoUrl'], track_id, 'Downloading track location XML',
                headers=self._api_headers(), fatal=False)
            if doc is None:
                continue
            host = xpath_text(doc, 'host')
            path = xpath_text(doc, 'path')
            ts = xpath_text(doc, 'ts')
            sign_salt = xpath_text(doc, 's')
            if not (host and path and ts and sign_salt):
                continue
            sign = hashlib.md5(
                (self._MP3_SALT + path[1:] + sign_salt).encode()).hexdigest()
            codec = info.get('codec') or 'mp3'
            formats.append({
                'url': f'https://{host}/get-mp3/{sign}/{ts}{path}',
                'format_id': join_nonempty(codec, int_or_none(info.get('bitrateInKbps'))),
                'ext': {'aac': 'm4a'}.get(codec, codec),
                'vcodec': 'none',
                'acodec': codec,
                'abr': int_or_none(info.get('bitrateInKbps')),
            })

        if not formats and only_preview and not self._get_token():
            self.raise_login_required(
                'Only a 30-second preview is available without authentication. '
                'Pass a Yandex OAuth token with '
                '--extractor-args "yandexmusic:token=YOUR_TOKEN" '
                '(obtain it via https://oauth.yandex.ru/authorize'
                '?response_type=token&client_id=23cabbbdc6cd418abb4b39c32c41195d)',
                method=None)
        return formats

    def _extract_artists(self, artists):
        names = traverse_obj(artists, (..., 'name', {str}))
        return ', '.join(names) or None

    def _track_info(self, track):
        track_id = str_or_none(track.get('id') or track.get('realId'))
        title = track['title']
        album = traverse_obj(track, ('albums', 0, {dict})) or {}

        cover_uri = track.get('coverUri') or album.get('coverUri')
        thumbnail = None
        if cover_uri:
            thumbnail = 'https://' + cover_uri.replace('%%', 'orig')

        artist = self._extract_artists(track.get('artists'))
        return {
            'id': track_id,
            'title': join_nonempty(artist, title, delim=' - '),
            'track': title,
            'artist': artist,
            'formats': self._extract_formats(track, track_id),
            'thumbnail': thumbnail,
            'duration': float_or_none(track.get('durationMs'), 1000),
            'filesize': int_or_none(track.get('fileSize')) or None,
            'album': album.get('title'),
            'album_artist': self._extract_artists(album.get('artists')),
            'release_year': int_or_none(album.get('year')),
            'genre': album.get('genre'),
            'disc_number': traverse_obj(album, ('trackPosition', 'volume', {int_or_none})),
            'track_number': traverse_obj(album, ('trackPosition', 'index', {int_or_none})),
        }


class YandexMusicTrackIE(YandexMusicBaseIE):
    IE_NAME = 'yandexmusic:track'
    IE_DESC = 'Яндекс.Музыка - Трек'
    _VALID_URL = rf'{YandexMusicBaseIE._VALID_URL_BASE}/album/(?P<album_id>\d+)/track/(?P<id>\d+)'

    _TESTS = [{
        'url': 'https://music.yandex.ru/album/40733359/track/148373155',
        'info_dict': {
            'id': '148373155',
            'ext': 'mp3',
            'title': 'BEARWOLF - Феникс',
            'track': 'Феникс',
            'artist': 'BEARWOLF',
            'album': 'Феникс',
            'album_artist': 'BEARWOLF',
            'release_year': 2026,
            'genre': 'ruspop',
            'duration': 172.26,
            'disc_number': 1,
            'track_number': 1,
            'thumbnail': r're:https?://.+',
        },
        'params': {'skip_download': True},
        'skip': 'Requires a Yandex OAuth token (see --extractor-args yandexmusic:token=...)',
    }, {
        'url': 'http://music.yandex.com/album/540508/track/4878838',
        'only_matching': True,
    }]

    def _real_extract(self, url):
        track_id = self._match_id(url)
        track = traverse_obj(
            self._call_api(f'tracks/{track_id}', track_id, 'Downloading track JSON'),
            (0, {dict}))
        if not track:
            raise ExtractorError('Unable to find track', expected=True)
        return self._track_info(track)


class YandexMusicPlaylistBaseIE(YandexMusicBaseIE):
    def _resolve_tracks(self, tracks, item_id):
        """Turn a list of (possibly short-info) playlist entries into full track dicts."""
        full, missing = [], []
        for entry in tracks:
            track = entry.get('track') if isinstance(entry, dict) else None
            if track:
                full.append(track)
                continue
            track_id = str_or_none(entry.get('id') if isinstance(entry, dict) else entry)
            if track_id:
                missing.append(track_id)

        # Bulk-resolve entries that did not ship an embedded track object
        for start in itertools.count(0, 250):
            chunk = missing[start:start + 250]
            if not chunk:
                break
            full.extend(self._call_api(
                'tracks', item_id, f'Downloading tracks JSON ({start + len(chunk)})',
                data=f'track-ids={",".join(chunk)}'.encode()) or [])
        return full

    def _build_playlist(self, tracks):
        for track in tracks:
            track_id = str_or_none(track.get('id') or track.get('realId'))
            album_id = traverse_obj(track, ('albums', 0, 'id', {str_or_none}))
            if not (track_id and album_id):
                continue
            yield self.url_result(
                f'https://music.yandex.ru/album/{album_id}/track/{track_id}',
                YandexMusicTrackIE, track_id,
                traverse_obj(track, ('title', {str})))


class YandexMusicAlbumIE(YandexMusicPlaylistBaseIE):
    IE_NAME = 'yandexmusic:album'
    IE_DESC = 'Яндекс.Музыка - Альбом'
    _VALID_URL = rf'{YandexMusicBaseIE._VALID_URL_BASE}/album/(?P<id>\d+)'

    _TESTS = [{
        'url': 'https://music.yandex.ru/album/40733359',
        'info_dict': {
            'id': '40733359',
            'title': 'BEARWOLF - Феникс (2026)',
        },
        'playlist_mincount': 1,
    }]

    @classmethod
    def suitable(cls, url):
        return False if YandexMusicTrackIE.suitable(url) else super().suitable(url)

    def _real_extract(self, url):
        album_id = self._match_id(url)
        album = self._call_api(
            f'albums/{album_id}/with-tracks', album_id, 'Downloading album JSON')

        tracks = [track for volume in album.get('volumes') or [] for track in volume]
        title = album.get('title')
        artist = traverse_obj(album, ('artists', 0, 'name', {str}))
        if artist:
            title = f'{artist} - {title}'
        if album.get('year'):
            title += f' ({album["year"]})'

        return self.playlist_result(
            self._build_playlist(tracks), str(album['id']), title)


class YandexMusicPlaylistIE(YandexMusicPlaylistBaseIE):
    IE_NAME = 'yandexmusic:playlist'
    IE_DESC = 'Яндекс.Музыка - Плейлист'
    _VALID_URL = rf'{YandexMusicBaseIE._VALID_URL_BASE}/users/(?P<user>[^/]+)/playlists/(?P<id>\d+)'

    _TESTS = [{
        'url': 'https://music.yandex.ru/users/music.partners/playlists/1245',
        'info_dict': {
            'id': '1245',
        },
        'playlist_mincount': 1,
    }, {
        'url': 'https://music.yandex.ru/users/ya.playlist/playlists/1036',
        'only_matching': True,
    }]

    def _real_extract(self, url):
        user, playlist_id = self._match_valid_url(url).group('user', 'id')
        playlist = self._call_api(
            f'users/{user}/playlists/{playlist_id}', playlist_id,
            'Downloading playlist JSON')

        tracks = self._resolve_tracks(playlist.get('tracks') or [], playlist_id)
        return self.playlist_result(
            self._build_playlist(tracks), playlist_id,
            playlist.get('title'), playlist.get('description'))


class YandexMusicArtistBaseIE(YandexMusicPlaylistBaseIE):
    def _artist_name(self, artist_id):
        return traverse_obj(self._call_api(
            f'artists/{artist_id}/brief-info', artist_id,
            'Downloading artist brief info', fatal=False),
            ('artist', 'name', {str}))


class YandexMusicArtistTracksIE(YandexMusicArtistBaseIE):
    IE_NAME = 'yandexmusic:artist:tracks'
    IE_DESC = 'Яндекс.Музыка - Артист - Треки'
    _VALID_URL = rf'{YandexMusicBaseIE._VALID_URL_BASE}/artist/(?P<id>\d+)/tracks'

    _TESTS = [{
        'url': 'https://music.yandex.ru/artist/21022190/tracks',
        'info_dict': {
            'id': '21022190',
        },
        'playlist_mincount': 1,
    }]

    def _real_extract(self, url):
        artist_id = self._match_id(url)
        tracks = []
        for page in itertools.count(0):
            data = self._call_api(
                f'artists/{artist_id}/tracks', artist_id,
                f'Downloading artist tracks page {page + 1}',
                query={'page': page, 'page-size': 100})
            page_tracks = data.get('tracks') or []
            tracks.extend(page_tracks)
            total = traverse_obj(data, ('pager', 'total', {int_or_none}))
            if not page_tracks or (total is not None and len(tracks) >= total):
                break

        artist = self._artist_name(artist_id)
        return self.playlist_result(
            self._build_playlist(tracks), artist_id,
            join_nonempty(artist or artist_id, 'Треки', delim=' - '))


class YandexMusicArtistAlbumsIE(YandexMusicArtistBaseIE):
    IE_NAME = 'yandexmusic:artist:albums'
    IE_DESC = 'Яндекс.Музыка - Артист - Альбомы'
    _VALID_URL = rf'{YandexMusicBaseIE._VALID_URL_BASE}/artist/(?P<id>\d+)/albums'

    _TESTS = [{
        'url': 'https://music.yandex.ru/artist/21022190/albums',
        'info_dict': {
            'id': '21022190',
        },
        'playlist_mincount': 1,
    }]

    def _real_extract(self, url):
        artist_id = self._match_id(url)
        albums = []
        for page in itertools.count(0):
            data = self._call_api(
                f'artists/{artist_id}/direct-albums', artist_id,
                f'Downloading artist albums page {page + 1}',
                query={'page': page, 'page-size': 100})
            page_albums = data.get('albums') or []
            albums.extend(page_albums)
            total = traverse_obj(data, ('pager', 'total', {int_or_none}))
            if not page_albums or (total is not None and len(albums) >= total):
                break

        entries = []
        for album in albums:
            album_id = traverse_obj(album, ('id', {str_or_none}))
            if album_id:
                entries.append(self.url_result(
                    f'https://music.yandex.ru/album/{album_id}',
                    YandexMusicAlbumIE, album_id))

        artist = self._artist_name(artist_id)
        return self.playlist_result(
            entries, artist_id, join_nonempty(artist or artist_id, 'Альбомы', delim=' - '))
