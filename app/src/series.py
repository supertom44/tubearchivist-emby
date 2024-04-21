"""set metadata to shows"""

import base64
import os
from time import sleep
import pprint

from src.config import get_config
from src.connect import Jellyfin, TubeArchivist, clean_overview
from src.episode import Episode
from src.static_types import JFEpisode, JFShow, TAChannel, TAVideo
import logging
from logging.handlers import RotatingFileHandler

logfile_name = '/app/logs/' + os.path.basename(__file__).split('.')[0] + '.log'
logging.basicConfig(
    handlers=[
        RotatingFileHandler(
            logfile_name,
            # Limit the size to 10000000Bytes ~ 10MB 
            maxBytes=10000000,
            backupCount=5
        )
    ],
    format='%(asctime)s %(levelname)-4s %(filename)s:%(funcName)s %(message)s',
    level=logging.INFO,
    datefmt='%Y-%m-%d %H:%M:%S')


class Library:
    """grouped series"""

    COLLECTION_ART = "assets/collection-art.jpg"

    def __init__(self) -> None:
        self.yt_collection: str = self.get_yt_collection()

    def get_yt_collection(self) -> str:
        """get collection id for youtube folder"""
        path: str = "Items?Recursive=true&includeItemTypes=Folder"
        folders: dict = Jellyfin().get(path)
        folder_name: str | None = get_config()["emby_folder"]

        if not folder_name or len(folder_name) < 1:
            folder_name = "youtube"
        else:
            folder_name = folder_name.lower()

        for folder in folders["Items"]:
            if folder.get("Name").lower() == folder_name:
                return folder.get("Id")

        raise ValueError("youtube folder not found")

    def validate_series(self) -> None:
        """validate all series"""
        collection_id: str = self._get_collection()
        self.refresh_collection(collection_id)
        all_shows: list[JFShow] = self._get_all_series()["Items"]
        for show in all_shows:
            logging.info(f"show: {show}")
            show_handler = Show(show)
            show_handler.validate_show()
            show_handler.validate_episodes()
            
        # remove collection art image, use default generate image
        #self.set_collection_art(collection_id)
        self.refresh_collection(collection_id)

    def _get_all_series(self) -> dict:
        """get all shows indexed in jf"""
        path: str = f"Items?Recursive=true&IncludeItemTypes=Series&fields=ParentId,Path&ParentId={self.yt_collection}"  # noqa: E501
        all_shows: dict = Jellyfin().get(path)

        return all_shows

    def _get_collection(self) -> str:
        """get youtube collection id"""
        folders: dict = Jellyfin().get("Library/MediaFolders")
        logging.info(f"folders: {folders}")
        for folder in folders["Items"]:
            if folder.get("Name") == "YouTube":
                logging.info(f"find youtube: {folder}")
                return folder["Id"]

        raise ValueError("youtube collection folder not found")

    def set_collection_art(self, collection_id: str) -> None:
        """set collection ta art"""
        with open(self.COLLECTION_ART, "rb") as f:
            asset: bytes = f.read()

        path: str = f"Items/{collection_id}/Images/Primary"
        Jellyfin().post_img(path, base64.b64encode(asset))

    def refresh_collection(self, collection_id: str) -> None:
        """trigger collection refresh"""
        path: str = f"Items/{collection_id}/Refresh?Recursive=true&ImageRefreshMode=Default&MetadataRefreshMode=Default"  # noqa: E501
        #Jellyfin().post(path, False)

        for _ in range(12):
            response = Jellyfin().get("Library/VirtualFolders")
            for folder in response:
                if not folder["ItemId"] == collection_id:
                    continue

                if folder.get("RefreshStatus","") == "Idle":
                    return

                print("waiting for library refresh")
                pprint.pp(folder)
                sleep(5)


class Show:
    """interact with a single show"""

    def __init__(self, show: JFShow):
        self.show: JFShow = show

    def validate_show(self) -> None:
        """set show metadata"""
        ta_channel: TAChannel | None = self._get_ta_channel()
        if ta_channel is None:
            return
        self.update_metadata(ta_channel)
        self.update_artwork(ta_channel)

    def _get_ta_channel(self) -> TAChannel | None:
        """get ta channel metadata"""
        channel_id: str = self.show["Path"].replace("\\", "/").split("/")[-1]
        ta_channel: TAChannel | None = TubeArchivist().get_channel(channel_id)

        return ta_channel

    def update_metadata(self, ta_channel: TAChannel) -> None:
        """update channel metadata"""
        path: str = "Items/" + self.show["Id"]
        data: dict = {
            "Id": self.show["Id"],
            "Name": ta_channel["channel_name"],
            "Overview": self._get_desc(ta_channel),
            "Genres": [],
            "Tags": [],
            "ProviderIds": {},
        }
        #logging.info(f"data: {data}")
        Jellyfin().post(path, data)

    def _get_desc(self, ta_channel: TAChannel) -> str | bool:
        """get parsed description"""
        raw_desc: str = ta_channel["channel_description"]
        if not raw_desc:
            return False

        desc_clean: str = clean_overview(raw_desc)

        return desc_clean

    def update_artwork(self, ta_channel: TAChannel) -> None:
        """set channel artwork"""
        jf_id: str = self.show["Id"]
        jf_handler = Jellyfin()

        primary = TubeArchivist().get_thumb(ta_channel["channel_thumb_url"])
        jf_handler.post_img(f"Items/{jf_id}/Images/Primary", primary)
        jf_handler.post_img(f"Items/{jf_id}/Images/Logo", primary)

        banner = TubeArchivist().get_thumb(ta_channel["channel_banner_url"])
        jf_handler.post_img(f"Items/{jf_id}/Images/Banner", banner)

        tvart = TubeArchivist().get_thumb(ta_channel["channel_tvart_url"])
        jf_handler.post_img(f"Items/{jf_id}/Images/Backdrop", tvart)

    def validate_episodes(self) -> list[str] | None:
        """sync all episodes"""
        showname: str = self.show["Name"]
        new_episodes: list[JFEpisode] = self._get_all_episodes(filter_new=True)
        if not new_episodes:
            print(f"[show][{showname}] no new videos found")
            return None

        print(f"[show][{showname}] indexing {len(new_episodes)} videos")
        for jf_ep in new_episodes:
            youtube_id: str = os.path.basename(
                jf_ep["Path"].replace("\\", "/")
            ).split(".")[0]
            logging.info(f"youtube_id: {youtube_id}")
            episode_handler = Episode(youtube_id, jf_ep["Id"])
            ta_video: TAVideo = episode_handler.get_ta_video()
            logging.info(f"will sync ta_videos: {ta_video}")
            episode_handler.sync(ta_video)
       
        return True

    def _get_all_episodes(self, filter_new: bool = False) -> list[JFEpisode]:
        """get all episodes of show"""
        series_id: str = self.show["Id"]
        path: str = f"Shows/{series_id}/Episodes?fields=Path,Studios"

        all_episodes = Jellyfin().get(path)
        all_items: list[JFEpisode] = all_episodes["Items"]
        logging.info(f"super all_items: {all_items}")

        if filter_new:
            all_items = [i for i in all_items if not i["Studios"]]
            #all_items = [i for i in all_items if not i["IndexNumber"]]

        return all_items

    def create_season(self, ta_video: TAVideo, jf_ep: JFEpisode) -> str | None:
        """create season folders"""
        existing_seasons = self._get_existing_seasons()
        expected_season = ta_video["published"].split("-")[0]
        published = ta_video["published"]
        logging.info(f"published: {published}, expected_season: {expected_season}, existing_seasons: {existing_seasons}")

        if expected_season in existing_seasons:
            return None

        base: str = get_config()["ta_video_path"]
        channel_folder = os.path.split(
            os.path.split(jf_ep["Path"].replace("\\", "/"))[0]
        )[-1]
        logging.info(f"channel_folder: {channel_folder}")

        season_folder = os.path.join(base, channel_folder, expected_season)
        if not os.path.exists(season_folder):
            original_umask = os.umask(0)
            try:
                os.mkdir(season_folder, mode=0o777)
                logging.info(f"mkdir season_folder: {season_folder}")

            finally:
                os.umask(original_umask)

        self._wait_for_season(expected_season)

        return season_folder

    def _wait_for_season(self, expected_season: str) -> None:
        """wait for season to be created in JF"""
        jf_id: str = self.show["Id"]
        path: str = f"Items/{jf_id}/Refresh?Recursive=true&ImageRefreshMode=Default&MetadataRefreshMode=Default"  # noqa: E501
        logging.info(f"path: {path}")
        refresh_item: dict = {
            "Recursive": True,
            "ImageRefreshMode": 'Default',
            "MetadataRefreshMode": 'Default',
        }
        res = Jellyfin().post(path, refresh_item)
        logging.info(f"post seanson refresh to emby api res: {res}")
        for _ in range(12):
            all_existing: set[str] = set(self._get_existing_seasons())
            logging.info(f"expected_season: {expected_season}, all_existing: {all_existing}")
            if expected_season in all_existing:
                return

            print(f"[setup][{jf_id}] waiting for seasons to be created")
            sleep(5)

        raise TimeoutError("timeout reached for creating season folder")

    def _get_existing_seasons(self) -> list[str]:
        """get all seasons indexed of series"""
        series_id: str = self.show["Id"]
        path: str = f"Shows/{series_id}/Seasons"
        all_seasons: dict = Jellyfin().get(path)
        logging.info(f"all_seasons: {all_seasons}")

        # fix 'Name': 'Season Unknown'
        for i in all_seasons["Items"]:
            if str(i.get('Name')) == 'Season Unknown':
                logging.info('Season Unknown')
        return [str(i.get("IndexNumber")) for i in all_seasons["Items"]]

    def delete_folders(self, folders: list[str]) -> None:
        """delete temporary folders created"""
        for folder in folders:
            if os.path.exists(folder):
                os.removedirs(folder)
                logging.info(f"removedirs fodler: {folder}")
