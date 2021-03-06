#! /usr/bin/env python3
# -*- coding: utf-8 -*-

import re
import sys
import json
import time
import uuid
import http
import math
import random
import urllib3
import requests
import tweepy


class RotateKeys(Exception):
    '''
    exception raised on api rate limits
    it is used for repacing the cursor or stream and
    creating them again with new credentials
    '''
    pass


class StreamListener(tweepy.StreamListener):
    def __init__(self, twitter, callback):
        self.twitter = twitter
        self.callback = callback
        self.search_id = twitter.search_id
        self.current_url = twitter.current_url
        self.type = twitter.type
        self.last_rotation = time.time()
        super().__init__()

    def on_status(self, entry):
        self.callback(self, from_tweepy_obj_to_json(entry))
        MINS_15 = 15 * 60
        # re-create the stream each 15 minutes to avoid rate limits
        if time.time() - self.last_rotation > MINS_15:
            self.last_rotation = time.time()
            self.__rotate_apikey()

    def on_error(self, status_code):
        self.__rotate_apikey()
        return True

    def on_timeout(self):
        self.__rotate_apikey()
        return True

    def __rotate_apikey(self):
        '''raise a RotateKeys exception if there is more than one key'''
        if len(self.twitter.twitter_apis) > 1:
            raise RotateKeys()


class Twitter:

    def __init__(self, twitter_apis):
        self.twitter_apis = twitter_apis
        if len(self.twitter_apis) == 0:
            exit('Provide at least one Twitter API key')
        for twitter_api in self.twitter_apis:
            twitter_api['expired_at'] = {}
        self.api_in_use = random.choice(self.twitter_apis)
        self.api = self.__authenticate(self.api_in_use)
        self.current_url = ''
        self.search_id = str(uuid.uuid4())
        self.type = ''

    def __query_api_with_cursor(self, url_used, entry_handler, apiname, **kwargs):
        '''queries a tweepy api using cursors handling errors and rate limits'''
        cursor = tweepy.Cursor(getattr(self.api, apiname),  **kwargs)
        while True:
            self.current_url = url_used
            try:
                for entry in self.__limit_handled(cursor.items()):
                    entry_handler(self, from_tweepy_obj_to_json(entry))
                return
            except RotateKeys:
                self.__handle_rate_limit()
                old_cursor = cursor
                cursor = tweepy.Cursor(getattr(self.api, apiname), **kwargs)
                cursor.iterator.next_cursor = old_cursor.iterator.next_cursor
                cursor.iterator.prev_cursor = old_cursor.iterator.next_cursor
                cursor.iterator.num_tweets = old_cursor.iterator.num_tweets
            except Exception as e:
                self.__handle_exception(e)

    def __query_api_with_stream(self, entry_handler, **kwargs):
        '''queries a tweepy api using streams handling errors and rate limits'''
        while True:
            try:
                stream_listener = StreamListener(self, entry_handler)
                stream = tweepy.Stream(auth=self.api.auth, listener=stream_listener)
                stream.filter(**kwargs)
                return
            except RotateKeys:
                self.__rotate_apikey()
            except Exception as e:
                self.__handle_exception(e)

    def __query_api_raw(self, url_used, api, *args, **kwargs):
        '''queries a tweepy api handling errors and rate limits'''
        while True:
            self.current_url = url_used
            try:
                result = api(*args, **kwargs)
                return from_tweepy_obj_to_json(result)
            except tweepy.RateLimitError:
                self.__handle_rate_limit()
            except Exception as e:
                self.__handle_exception(e)

    def __handle_exception(self, e):
        '''handle many known from tweepy/twitter'''
        msg = str(e)

        # exception commonly thrown by cursors
        if type(e) == http.client.IncompleteRead:
            time.sleep(5)
        # exception commonly thrown by cursors
        elif type(e) == requests.exceptions.ConnectionError:
            time.sleep(5)
        # exception commonly thrown by strams
        elif type(e) == urllib3.exceptions.ProtocolError:
            time.sleep(5)
        # tweepy failed to send the request
        elif 'Failed to send request' in msg:
            time.sleep(5)

        # twitter specific exception
        # https://developer.twitter.com/en/support/twitter-api/error-troubleshooting

        # 429
        # Returned when a request cannot be served due to the App's rate limit having been exhausted for the resource.
        elif 'Too Many Requests' in msg:
            self.__handle_rate_limit()
        # 503
        # The Twitter servers are up, but overloaded with requests. Try again later.
        elif 'Service Unavailable' in msg:
            time.sleep(60)
        # 89
        # Corresponds with HTTP 403. The access token used in the request is incorrect or has expired.
        elif 'Invalid or expired token' in msg:
            invalid_api_key = self.api_in_use
            print('got \'Invalid or expired token\' error')
            print('is this api key valid?\n{}'.format(json.dumps(invalid_api_key, indent=4, sort_keys=True)))
            if len(self.twitter_apis) == 1:
                sys.exit()
            else:
                print('removing api key...')
                self.__rotate_apikey()
                self.twitter_apis = [key for key in self.twitter_apis if key != invalid_api_key]
        else:
            # print('got unknown error: {}'.format(str(e)))
            raise e

    def get_retweeters(self, tweet_id, entry_handler):
        '''returns up to 100 user IDs that have retweeted the tweet'''
        # https://developer.twitter.com/en/docs/twitter-api/v1/tweets/post-and-engage/api-reference/get-statuses-retweeters-ids
        count = 100
        request_per_window = 300
        if self.type == '':
            self.type = 'get_retweeters'

        user_id = self.statuses_lookup([tweet_id])[0]['user']['id']

        def callback(obj, retweeter_uid):
            entry = {}
            entry['retweeted_tid'] = int(tweet_id)
            entry['retweeted_uid'] = user_id
            entry['retweeter_uid'] = retweeter_uid
            entry_handler(self, entry)

        self.__query_api_with_cursor('/statuses/retweeters/ids', callback, 'retweeters', id=tweet_id, count=count)

    def statuses_lookup(self, tweet_ids):
        '''returns detail data from a list of tweet ids'''
        # https://developer.twitter.com/en/docs/tweets/post-and-engage/api-reference/get-statuses-lookup
        max_per_request = 100
        request_per_window = 300
        if self.type == '':
            self.type = 'statuses_lookup'

        tweets_data = []
        while len(tweet_ids) > 0:
            batch = tweet_ids[:max_per_request]
            tweet_ids = tweet_ids[max_per_request:]
            tweets_data += self.__query_api_raw('/statuses/lookup', self.api.statuses_lookup, batch)
        return tweets_data

    def get_retweeters_new(self, tweet_ids, entry_handler):
        '''returns the new retweeters from a given tweet'''
        if self.type == '':
            self.type = 'get_retweeters_new'

        user_ids = [tweet['user']['id_str'] for tweet in self.statuses_lookup(tweet_ids)]

        def callback(obj, tweet):
            if 'retweeted_status' not in tweet:
                return
            if tweet['retweeted_status']['id_str'] not in tweet_ids:
                return
            entry_handler(self, tweet)

        self.get_timeline_new(user_ids, callback)

    def get_followers(self, user, entry_handler, max_results=None):
        '''returns the followers of a user, ordered from new to old'''
        # https://developer.twitter.com/en/docs/twitter-api/v1/accounts-and-users/follow-search-get-users/api-reference/get-followers-list
        count = 200
        request_per_window = 15
        if self.type == '':
            self.type = 'get_followers'
        profile = self.get_profile(user)
        if profile['protected']:
            print('the account {} is not public'.format(profile['screen_name']))
            return
        if max_results is None:
            max_results = profile['followers_count']
        followers_count = min(max_results, profile['followers_count'])
        self.__show_running_time(followers_count, count, request_per_window)
        number_of_followers = 0

        def callback(obj, entry):
            nonlocal number_of_followers
            entry['follows'] = profile
            entry['follower_number'] = profile['followers_count'] - number_of_followers
            entry_handler(self, entry)
            number_of_followers += 1

        self.__query_api_with_cursor('/followers/list', callback, 'followers', id=user, count=count)

    def get_friends(self, user, entry_handler, max_results=None):
        '''returns the users that the user follows'''
        # https://developer.twitter.com/en/docs/twitter-api/v1/accounts-and-users/follow-search-get-users/api-reference/get-friends-list
        count = 200
        request_per_window = 15
        if self.type == '':
            self.type = 'get_friends'
        profile = self.get_profile(user)
        if profile['protected']:
            print('the account {} is not public'.format(profile['screen_name']))
            return
        if max_results is None:
            max_results = profile['friends_count']
        friends_count = min(max_results, profile['friends_count'])
        self.__show_running_time(friends_count, count, request_per_window)
        user_id = self.__name_to_id([user])[0]

        def callback(obj, entry):
            entry['is_followed_by'] = profile
            entry_handler(self, entry)

        self.__query_api_with_cursor('/friends/list', callback, 'friends', id=user_id, count=count)

    def get_profile(self, user):
        '''returns the profile information of a user'''
        # https://developer.twitter.com/en/docs/twitter-api/v1/accounts-and-users/follow-search-get-users/api-reference/get-users-show
        request_per_window = 900
        if self.type == '':
            self.type = 'get_profile'

        return self.__query_api_raw('/users/show', self.api.get_user, id=user)

    def get_timeline(self, user, entry_handler, max_results=None):
        '''returns up to 3.200 of a user's most recent tweets'''
        # https://developer.twitter.com/en/docs/twitter-api/v1/tweets/timelines/api-reference/get-statuses-user_timeline
        count = 200
        request_per_window = 1500
        if self.type == '':
            self.type = 'get_timeline'
        profile = self.get_profile(user)
        if profile['protected']:
            print('the account {} is not public'.format(profile['screen_name']))
            return
        if max_results is None:
            max_results = profile['statuses_count']
        statuses_count = min(3200, max_results, profile['statuses_count'])
        self.__show_running_time(statuses_count, count, request_per_window)
        if self.type == '':
            self.type = 'get_timeline'

        def callback(obj, entry):
            entry['type'] = 'tweet' if 'retweeted_status' not in entry else 'retweet'
            entry_handler(self, entry)

        self.__query_api_with_cursor('/statuses/user_timeline', callback, 'user_timeline', id=user, include_rts=True, count=count)

    def get_timeline_new(self, users, entry_handler, get_all=False):
        '''returns new tweets of a list of users'''
        if self.type == '':
            self.type = 'get_timeline_new'
        user_ids = []
        for user in users:
            profile = self.get_profile(user)
            if profile['protected']:
                print('the account {} is not public'.format(profile['screen_name']))
                return
            user_ids.append(profile['id_str'])

        def callback(obj, entry):
            if get_all or entry['user']['id_str'] in user_ids:
                entry['type'] = 'tweet' if 'retweeted_status' not in entry else 'retweet'
                entry_handler(self, entry)

        self.__query_api_with_stream(callback, follow=user_ids)

    def search(self, q, entry_handler):
        '''searches for already published tweets that match the search'''
        # https://developer.twitter.com/en/docs/twitter-api/v1/tweets/search/api-reference/get-search-tweets
        count = 100
        request_per_window = 450
        if self.type == '':
            self.type = 'search'

        def callback(obj, entry):
            entry['search'] = q
            entry_handler(self, entry)

        self.__query_api_with_cursor('/search/tweets', callback, 'search', q=q, count=count)

    def stream_callback(self, obj, tweet, entry_handler):
        is_retweet = 'retweeted_status' in tweet
        is_reply = tweet['in_reply_to_status_id_str'] is not None
        is_quote = 'quoted_status' in tweet

        if is_retweet:
            entry = {}
            entry['type'] = 'retweet'
            entry['tweet'] = tweet
            entry_handler(self, entry)
        elif is_reply:
            entry = {}
            entry['type'] = 'reply'
            entry['tweet'] = tweet
            entry['replied_to'] = self.statuses_lookup([tweet['in_reply_to_status_id_str']])[0]
            entry_handler(self, entry)
        elif is_quote:
            entry = {}
            entry['type'] = 'quote'
            entry['tweet'] = tweet
            entry_handler(self, entry)
        else:
            entry = {}
            entry['type'] = 'tweet'
            entry['tweet'] = tweet
            entry_handler(self, entry)

    def search_new(self, q, entry_handler):
        '''returns new tweets that match the search'''
        if self.type == '':
            self.type = 'search_new'

        def callback(obj, entry):
            self.stream_callback(obj, entry, entry_handler)

        self.__query_api_with_stream(callback, track=q.split(' '))

    def watch(self, users, entry_handler):
        '''saves all the tweets and its retweets for a list of users'''
        if self.type == '':
            self.type = 'watch'
        user_ids = []
        for user in users:
            profile = self.get_profile(user)
            if profile['protected']:
                print('the account {} is not public'.format(profile['screen_name']))
                return
            user_ids.append(profile['id_str'])

        def callback(obj, entry):
            self.stream_callback(obj, entry, entry_handler)

        self.get_timeline_new(user_ids, callback, get_all=True)

    def __name_to_id(self, id_name_list):
        '''takes a list of usernames and returns a list of ids'''
        id_list = []
        for id_name in id_name_list:
            if re.search(r'^\d+$', id_name):
                id_list.append(id_name)
            else:
                profile = self.get_profile(id_name)
                id_list.append(str(profile['id']))
        return id_list

    def __show_running_time(self, records_amount, count, request_per_window):
        '''used for calculating running time on some queries'''
        apis_amount = len(self.twitter_apis)
        records_per_round = count * request_per_window * apis_amount
        rounds_needed = math.ceil(records_amount / records_per_round)
        minutes = (rounds_needed - 1) * 15

        if minutes == 0:
            return
        hours = int((minutes // 60) % 24)
        days = int(minutes // 1440)
        minutes = int(minutes % 60)
        msg = ''
        if days > 0:
            s = '' if days == 1 else 's'
            msg += '{} day{} '.format(days, s)
        if hours > 0:
            s = '' if hours == 1 else 's'
            msg += '{} hour{} '.format(hours, s)
        if minutes > 0:
            s = '' if minutes == 1 else 's'
            msg += '{} minute{} '.format(minutes, s)
        msg = msg[:-1]
        print('this will take approximately {} to finish'.format(msg))

    def __authenticate(self, credentials):
        '''authenticates to twitter with the given credentials'''
        auth = tweepy.OAuthHandler(credentials['CONSUMER_KEY'], credentials['CONSUMER_SECRET'])
        auth.set_access_token(credentials['ACCESS_KEY'], credentials['ACCESS_SECRET'])
        api = tweepy.API(auth)
        return api

    def __rotate_apikey(self):
        '''used for streams, rotates the api key being used'''
        if len(self.twitter_apis) == 1:
            return
        for i, twitter_api in enumerate(self.twitter_apis):
            if twitter_api == self.api_in_use:
                self.api_in_use = self.twitter_apis[(i + 1) % len(self.twitter_apis)]
                self.api = self.__authenticate(self.api_in_use)
                return
        raise Exception('API Key not found')

    def __limit_handled(self, cursor):
        '''used by cursors, handles exceptions and rate limits'''
        while True:
            try:
                yield cursor.next()
            except StopIteration:
                return
            except tweepy.RateLimitError:
                raise RotateKeys()
            except tweepy.error.TweepError as e:
                if 'status code = 429' in str(e):
                    raise RotateKeys()
                elif 'Failed to send request' in str(e):
                    time.sleep(5)
                    continue
                else:
                    print('got unknown error: {}'.format(str(e)))
                    time.sleep(5)
                    continue

    def __handle_rate_limit(self):
        '''
        changes the API key being used based on their expiration status
        if all keys are expired, it waits the smallest amount of time possible
        '''
        now = time.time()
        self.api_in_use['expired_at'][self.current_url] = now

        if len(self.twitter_apis) == 1:
            time.sleep(15 * 60)
            return

        valid_apis = [twitter_api for twitter_api in self.twitter_apis if self.current_url not in twitter_api['expired_at'] or now - twitter_api['expired_at'][self.current_url] >= 15 * 60]
        if len(valid_apis) > 0:
            self.api_in_use = valid_apis[0]
            self.api = self.__authenticate(self.api_in_use)
        else:
            self.twitter_apis.sort(reverse=False, key=lambda api: api['expired_at'][self.current_url])
            self.api_in_use = self.twitter_apis[0]
            time_expired = now - self.api_in_use['expired_at'][self.current_url]
            time.sleep(15 * 60 - time_expired + 3)
            self.api = self.__authenticate(self.api_in_use)


def from_tweepy_obj_to_json(tweepy_obj):
    '''converts a tweepy object to a json object'''
    if type(tweepy_obj) == list:
        return [from_tweepy_obj_to_json(entry) for entry in tweepy_obj]
    elif type(tweepy_obj) == tweepy.models.ResultSet:
        return [from_tweepy_obj_to_json(entry) for entry in tweepy_obj]
    elif hasattr(tweepy_obj, '_json'):
        return tweepy_obj._json
    else:
        return tweepy_obj
