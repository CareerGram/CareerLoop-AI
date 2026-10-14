
import re
import unittest


# Keep these tests independent of Streamlit and Gmail.
STOP_WORDS = {
    "the", "and", "for", "with", "you", "your", "are", "our",
    "this", "that", "from", "will", "have", "has", "into", "who",
    "what", "when", "where", "about", "using", "use", "can",
    "should", "must", "their", "they", "which", "was", "were",
    "been", "being", "a", "an", "of", "to", "in", "on", "at",
    "by", "is", "as", "or", "be", "we", "it",
}


def get_keywords(text):
    words = re.findall(
        r"[a-zA-Z][a-zA-Z0-9+#.-]*",
        text.lower(),
    )

    return {
        word for word in words
        if len(word) > 2 and word not in STOP_WORDS
    }


def calculate_match_score(resume_text, job_description):
    resume_words = get_keywords(resume_text)
    job_words = get_keywords(job_description)

    if not job_words:
        return 0.0

    return round(
        len(resume_words.intersection(job_words))
        / len(job_words)
        * 100,
        1,
    )


class MatchScoreTests(unittest.TestCase):

    def test_all_keywords_match(self):
        self.assertEqual(
            calculate_match_score(
                "Python data analysis",
                "Python data analysis",
            ),
            100.0,
        )

    def test_no_keywords_match(self):
        self.assertEqual(
            calculate_match_score(
                "Java backend",
                "Python data analysis",
            ),
            0.0,
        )

    def test_empty_resume(self):
        self.assertEqual(
            calculate_match_score(
                "",
                "Python data analysis",
            ),
            0.0,
        )

    def test_empty_job_description(self):
        self.assertEqual(
            calculate_match_score(
                "Python data analysis",
                "",
            ),
            0.0,
        )


if __name__ == "__main__":
    unittest.main()
