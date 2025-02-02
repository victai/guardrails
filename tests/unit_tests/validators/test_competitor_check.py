import unittest
from unittest.mock import MagicMock, patch

from guardrails.validators import CompetitorCheck, FailResult


class TestCompetitorCheck(unittest.TestCase):
    def setUp(self):
        # You can initialize any required resources or configurations here.
        pass

    def tearDown(self):
        # You can clean up any resources used during the tests here.
        pass

    @patch("spacy.load")
    def test_perform_ner(self, mock_spacy_load):
        # Create a mock NLP object
        mock_nlp = MagicMock()
        mock_spacy_load.return_value = mock_nlp

        # Mock the doc.ents property
        mock_nlp.return_value.ents = [MagicMock(text="Apple"), MagicMock(text="Google")]

        # Test the perform_ner method with spacy mocked
        competitors = ["Apple", "Microsoft", "Google"]
        validator = CompetitorCheck(competitors)

        text_with_entities = "I have an Apple laptop and a Google phone."
        entities = validator.perform_ner(text_with_entities, mock_nlp)
        self.assertEqual(entities, ["Apple", "Google"])

        del validator

    @patch("spacy.load")
    def test_validator_with_match_and_ner(self, mock_spacy_load):
        # Create a mock NLP object
        mock_nlp = MagicMock()
        mock_spacy_load.return_value = mock_nlp

        # Mock the doc.ents property
        mock_nlp.return_value.ents = [MagicMock(text="Microsoft")]

        # Test the CompetitorCheck validator with a matching text and mocked NER
        competitors = ["Apple", "Microsoft", "Google"]
        validator = CompetitorCheck(competitors)

        text_with_entities_and_match = (
            "I love my Microsoft laptop and use Microsoft Office."
        )
        result = validator.validate(text_with_entities_and_match)
        expected_fail_result = FailResult(
            outcome="fail",
            metadata=None,
            error_message="""Found the following competitors: [['Microsoft']].\
 Please avoid naming those competitors next time""",
            fix_value="",
        )

        self.assertEqual(result, expected_fail_result)


if __name__ == "__main__":
    unittest.main()
