import json
import os
import random
from datetime import datetime, timezone

import bittensor as bt
import openai
import torch

from .utils import ndcg_score, parse_llm_result, tweet_url_to_id


class Evaluator:
    def __init__(self, llm_client, twitter_crawler=None) -> None:
        # for ranking results evaluation
        self.llm_client = llm_client

        # for integrity check
        self.twitter_crawler = twitter_crawler

    def evaluate(self, query_string: str, size: int, responses: list):

        scores = torch.zeros(len(responses))

        zero_score_mask = torch.ones(len(responses))

        rank_scores = torch.zeros(len(responses))

        avg_ages = torch.zeros(len(responses))
        avg_age_scores = torch.zeros(len(responses))
        now = datetime.now(timezone.utc)
        max_avg_age = 0
        for i, response in enumerate(responses):
            try:
                if response is None or not response or len(response) > size:
                    zero_score_mask[i] = 0
                    continue
                if not self.check_integrity(response):
                    zero_score_mask[i] = 0
                    continue
                for doc in response:
                    avg_ages[i] += (
                        now - datetime.fromisoformat(doc["created_at"].rstrip("Z"))
                    ).total_seconds()
                avg_ages[i] /= len(response)
                max_avg_age = max(max_avg_age, avg_ages[i])

                rank_scores[i] = self.llm_ranking_evaluation(query_string, response)
            except Exception as e:
                bt.logging.error(f"Error while processing {i}-th response: {e}")
                zero_score_mask[i] = 0
        avg_age_scores = 1 - (avg_ages / (max_avg_age + 1))
        scores = avg_age_scores * 0.2 + rank_scores * 0.8

        return scores * zero_score_mask

    def check_integrity(self, response):
        """
        This function checks the integrity of the response.
        """
        try:
            for doc in response:
                doc_id = doc["id"]
                url_id = tweet_url_to_id(doc["url"])
                if doc_id != url_id:
                    bt.logging.error(
                        f"Document id {doc_id} does not match url id {url_id}"
                    )
                    return False

            # spot check with one document
            if self.twitter_crawler is None:
                bt.logging.warning(
                    "Twitter crawler is not initialized. spot content check is skipped."
                )
            else:
                spot_check = random.choice(response)
                r = self.twitter_crawler.get_tweet_by_url(spot_check["url"], 20)
                if not r:
                    bt.logging.error(
                        f"Failed to get tweet from url {spot_check['url']}"
                    )
                    return False
                ground_truth_doc = self.twitter_crawler.process_item(r)
                check_fields = ["text", "username"]
                for field in check_fields:
                    if spot_check[field] != ground_truth_doc[field]:
                        bt.logging.error(
                            f"Document {field} {spot_check[field]} does not match ground truth {ground_truth_doc[field]}"
                        )
                        return False
                if datetime.fromisoformat(
                    spot_check["created_at"].rstrip("Z")
                ) != datetime.fromisoformat(ground_truth_doc["created_at"].rstrip("Z")):
                    bt.logging.error(
                        f"Document created_at {spot_check['created_at']} does not match ground truth {ground_truth_doc['created_at']}"
                    )
                    return False
            bt.logging.debug(f"Integrity check passed for response: {response}")
            return True
        except Exception as e:
            bt.logging.error(f"Error while checking integrity of response: {e}")
            return False

    def llm_ranking_evaluation(self, query_string, docs, retries=3):
        """
        This function evaluates the ranking of the documents using the LLM.
        """
        try:
            newline = "\n"
            prompt_docs = "\n\n".join(
                [
                    f"ItemId: {i}\nTime: {doc['created_at'].split('T')[0]}\nText: {doc['text'][:1000].replace(newline, '  ')}"
                    for i, doc in enumerate(docs)
                ]
            )
            bt.logging.debug(
                f"Querying LLM of {query_string} with docs:\n" + prompt_docs
            )
            output = self.llm_client.chat.completions.create(
                model="gpt-4-0125-preview",
                response_format={"type": "json_object"},
                messages=[
                    {
                        "role": "system",
                        "content": """Below are the metrics and definations: 
    Outdated: Time-sensitive information that is no longer current or relevant.
    Off topic: Superficial content lacking depth and comprehensive insights.
    Somewhat Relevant: Offers partial insight but lacks depth and comprehensive coverage.
    Relevant: Comprehensive, insightful content suitable for informed decision-making.""",
                    },
                    {
                        "role": "system",
                        "content": f"Current Time: {datetime.now().isoformat().split('T')[0]}",
                    },
                    {
                        "role": "system",
                        "content": """
    Example 1:
    ItemId: 0
    Time: "2023-11-25" 
    Text: Also driving the charm is Blast's unique design: Depositors start earning yields on the transferred ether alongside BLAST points. "Blast natively participates in ETH staking, and the staking yield is passed back to the L2's users and dapps," the team said in a post Tuesday. 'We've redesigned the L2 from the ground up so that if you have 1 ETH in your wallet on Blast, over time, it grows to 1.04, 1.08, 1.12 ETH automatically."
    As such, Blast is invite-only as of Tuesday, requiring a code from invited users to gain access. Besides, the BLAST points can be redeemed starting in May.Blast raised over $20 million in a round led by Paradigm and Standard Crypto and is headed by pseudonymous figurehead @PacmanBlur, one of the co-founders of NFT marketplace Blur.
    @PacmanBlur said in a separate post that Blast was an extension of the Blur ecosystem, letting Blur users earn yields on idle assets while improving the technical aspects required to offer sophisticated NFT products to users.
    BLUR prices rose 12%% in the past 24 hours following the release of Blast

    Query: Blast

    Output:
    item_id: 0
    choice: relevant
    reason: It is relevant as it deep dives into the Blast project.

    Example 2:
    ItemId: 1
    Time: "2023-11-15"
    Text: To celebrate, we've teamed up with artist @debbietea8 to release a commemorative piece of art on @arbitrum! 😍
    Now available for free, exclusively in app! 🥳

    Query: Arbitrum

    Output:
    item_id: 1
    choice: off topic
    reason: It is not directly related to Arbitrum as it just uses the arbitrum app.
    """,
                    },
                    {
                        "role": "user",
                        "content": f"You will be given a list of documents with id and you have to rate them based on the relevance to the query. The documents are as follows:\n"
                        + prompt_docs,
                    },
                    {
                        "role": "user",
                        "content": f"Use the metric choices [outdated, off topic, somewhat relevant, relevant] to evaluate the text toward '{query_string}'?",
                    },
                    {
                        "role": "user",
                        "content": "Must answer in JSON format of a list of choices with item ids for all the given items: "
                        + "{'results': [{'item_id': the item id of choice, e.g. 0, 'reason': a very short explanation of your choice, 'choice':The choice of answer. }, {'item_id': 1, 'reason': explanation, 'choice': answer } , ... ] } ",
                    },
                ],
                temperature=0,
            )
            bt.logging.debug(f"LLM response: {output.choices[0].message.content}")
            bt.logging.debug(
                f"LLM usage: {output.usage}, finish reason: {output.choices[0].finish_reason}"
            )
        except Exception as e:
            bt.logging.error(f"Error while querying LLM: {e}")
            return 0

        try:
            result = json.loads(output.choices[0].message.content)
            bt.logging.debug(f"LLM result: {result}")
            ranking = parse_llm_result(result)
            bt.logging.info(f"LLM ranking: {ranking}")
            if len(ranking) != len(docs):
                raise ValueError(
                    f"Length of ranking {len(ranking)} does not match input docs length {len(docs)}"
                )
            ranking_score = ndcg_score(ranking)
            # ranking_score = dcg_score(ranking)
            bt.logging.info(f"LLM Ranking score: {ranking_score}")
            return ranking_score
        except Exception as e:
            bt.logging.error(f"Error while parsing LLM result: {e}, retrying...")
            if retries > 0:
                return self.llm_ranking_evaluation(query_string, docs, retries - 1)
            else:
                bt.logging.error(
                    f"Failed to parse LLM result after retrying. Returning 0."
                )
            return 0
