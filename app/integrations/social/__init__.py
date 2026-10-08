from app.integrations.social.bluesky import BlueskyProvider
from app.integrations.social.discord import DiscordProvider
from app.integrations.social.facebook import FacebookProvider
from app.integrations.social.instagram import InstagramProvider
from app.integrations.social.kick import KickProvider
from app.integrations.social.linkedin import LinkedinProvider
from app.integrations.social.mastodon import MastodonProvider
from app.integrations.social.medium import MediumProvider
from app.integrations.social.pinterest import PinterestProvider
from app.integrations.social.reddit import RedditProvider
from app.integrations.social.slack import SlackProvider
from app.integrations.social.telegram import TelegramProvider
from app.integrations.social.tiktok import TiktokProvider
from app.integrations.social.threads import ThreadsProvider
from app.integrations.social.tumblr import TumblrProvider
from app.integrations.social.twitch import TwitchProvider
from app.integrations.social.vk import VkProvider
from app.integrations.social.x import XProvider
from app.integrations.social.youtube import YoutubeProvider

PROVIDERS = {
    "linkedin": LinkedinProvider,
    "medium": MediumProvider,
    "discord": DiscordProvider,
    "telegram": TelegramProvider,
    "reddit": RedditProvider,
    "slack": SlackProvider,
    "mastodon": MastodonProvider,
    "pinterest": PinterestProvider,
    "youtube": YoutubeProvider,
    "facebook": FacebookProvider,
    "instagram": InstagramProvider,
    "x": XProvider,
    "tiktok": TiktokProvider,
    "bluesky": BlueskyProvider,
    "threads": ThreadsProvider,
    "tumblr": TumblrProvider,
    "vk": VkProvider,
    "kick": KickProvider,
    "twitch": TwitchProvider,
}
