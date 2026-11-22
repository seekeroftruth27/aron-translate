"""Quotation marks across captions, and the Router's pronoun question.

  - the translator's marks follow the source line (app/index.html,
    quoteFollowSource), on the lines that went wrong on 2026-11-15;
  - the quotation judge's wall edit (quoteCloseBack): the closing mark moves to
    the caption before, and the late one is dropped;
  - router_gate.quote_check and decide()'s "pronoun" reason;
  - /quote-check and /caps without a key.

The page's quotation code is cut out of the real page and run in the embedded
engine (mini-racer, see tests/test_page.py). No key is read, no provider called.

    pip install -r requirements-dev.txt
    python -m unittest discover -s tests -v
"""
import json
import os
import sys
import unittest
from unittest import mock

from support import load_server, need_flask

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import router_gate as rg  # noqa: E402

try:
    from py_mini_racer import MiniRacer
except ImportError:                                         # pragma: no cover
    MiniRacer = None
need_js = unittest.skipUnless(MiniRacer, "needs mini-racer (pip install -r requirements-dev.txt)")


def quote_source():
    """From the quotation section's head to the next section: quoteEdit ...
    quoteCloseBack, as the page has them."""
    with open(os.path.join(ROOT, "app", "index.html"), encoding="utf-8") as f:
        html = f.read()
    a = html.index("// ---- quotation marks across captions")
    return html[a:html.index("// ---- Soniox's own translation keeps the hesitations", a)]


HARNESS = """
var logs = [], seq = 0, lastCap = null, caps = {}, J = {rows: [], dirty: false};
function wallDead(){ return false; }
function castSeg(){} function publishSeg(){}
function log(level, msg){ logs.push(msg); }
function cap(text){ return {isConnected: true, childNodes: [{nodeType: 3, nodeValue: text}]}; }
// nvShow's part in it: quoteFix, show, quoteBind -- and what the judge is asked about
function show(text, lang){
  lang = lang || 'en';
  var was = qOpen, prev = lastCap;
  text = quoteFix(text, lang);
  var el = cap(text), segId = 's' + (++seq);
  lastCap = {el: el, segId: segId, tgt: lang};
  quoteBind(el, segId, lang, text);
  return {el: el, segId: segId, was: was, prev: prev, shut: qShut,
          asked: !!(was && (qOpen === was || qShut >= 0))};
}
function text(c){ return c.el.childNodes[0].nodeValue; }
"""


@need_js
class FollowTheSource(unittest.TestCase):
    """A Router-sent line keeps only the quotation marks reaching past it that its
    source has; 2026-11-15's lines (session rows in the comments)."""

    def setUp(self):
        self.js = MiniRacer()
        self.js.eval(HARNESS + quote_source())

    def follow(self, tgt, src, lang="en"):
        return self.js.eval("quoteFollowSource(%s, %s, %s)" % (json.dumps(tgt), json.dumps(src), json.dumps(lang)))

    def test_an_opener_the_source_lacks_goes(self):            # 427
        self.assertEqual(self.follow("The blind man answered Him, “Rabboni,", "Rabunii i-a răspuns orbul:"),
                         "The blind man answered Him, Rabboni,")
        self.assertEqual(self.follow("și spune: „fără speranță", "and says without hope", "ro"),   # 527
                         "și spune: fără speranță")

    def test_a_closer_the_source_lacks_goes(self):             # 2242, 481
        self.assertEqual(self.follow("until he has settled the matter today.”",
                                     "până nu va sfârși lucrul chiar azi,"),
                         "until he has settled the matter today.")
        self.assertEqual(self.follow("împreună cu Hristos.”", "together with Christ.", "ro"), "împreună cu Hristos.")

    def test_the_sources_closer_comes_back(self):              # 487: a hymn verse left open for 60 words
        self.assertEqual(self.follow("for Him to soothe.", "să nu o aline.»"), "for Him to soothe.”")
        self.assertEqual(self.follow("for Him to soothe.", 'să nu o aline."'), "for Him to soothe.”")

    def test_its_own_quotation_inside_the_line_stays(self):
        for tgt, src in (("“My dear daughters,” Naomi says to them,", "Dragele mele fete, îi spune Naomi,"),   # 1859
                         ("“There is no God.”", 'că nu este Dumnezeu."'),                                   # 1527
                         ("you can also find “special donation”", "mai găsiți donație specială")):           # 2383
            self.assertEqual(self.follow(tgt, src), tgt)

    def test_marks_the_source_has_stay(self):
        self.assertEqual(self.follow("“If a wicked servant who says in his heart:",
                                     '"Dacă un rob rău care zice în inima lui:'),
                         "“If a wicked servant who says in his heart:")
        # a closer the source has at the start of its line is not moved
        self.assertEqual(self.follow("We see here, further on,", "» Vedem aici, mai departe,"),
                         "We see here, further on,")


@need_js
class EachLanguagesMarks(unittest.TestCase):
    """quoteFix writes each language's own quotation marks, whichever the writer
    typed, and keeps tracking them across captions."""

    def setUp(self):
        self.js = MiniRacer()
        self.js.eval(HARNESS + quote_source())

    def fix(self, text, lang):
        return self.js.eval("quoteFix(%s, %s)" % (json.dumps(text), json.dumps(lang)))

    def test_one_style_per_language(self):
        for lang, want in (("en", "He said: “Lord, have mercy!”"), ("ro", "He said: „Lord, have mercy!”"),
                           ("de", "He said: „Lord, have mercy!“"), ("fr", "He said: «Lord, have mercy!»"),
                           ("ja", "He said: 「Lord, have mercy!」"), ("pt", "He said: “Lord, have mercy!”")):
            for typed in ('He said: "Lord, have mercy!"', "He said: “Lord, have mercy!”",
                          "He said: «Lord, have mercy!»", "He said: „Lord, have mercy!“"):
                self.assertEqual(self.fix(typed, lang), want, (lang, typed))

    def test_french_spacing_is_kept(self):
        self.assertEqual(self.fix("Il dit : « Seigneur, aie pitié ! »", "fr"), "Il dit : « Seigneur, aie pitié ! »")

    def test_a_german_quotation_across_captions(self):
        self.assertEqual(self.js.eval("text(show('Er sagt: „Herr, erbarme dich', 'de'))"), "Er sagt: „Herr, erbarme dich")
        c = self.js.eval("var c = show('über mich.“ Und dann', 'de'); [text(c), c.asked, c.shut]")
        self.assertEqual(list(c), ["über mich.“ Und dann", True, 0])
        self.assertIsNone(self.js.eval("qOpen"))
        # and a closer with nothing open is still dropped
        self.assertEqual(self.fix("über mich.“", "de"), "über mich.")


@need_js
class JudgeClosesBack(unittest.TestCase):
    """What the page does with the judge's "close": the caption before gets the
    closing mark, and the newest loses the late one."""

    def setUp(self):
        self.js = MiniRacer()
        self.js.eval(HARNESS + quote_source())

    def test_commentary_after_a_question(self):
        # 10:07:24: Mark 10:51, then four lines of his own inside the quotation
        self.js.eval("""
          var a = show('Further on, the Lord Jesus tells him:');
          var b = show('“What do you want me to do to you?');
          var c = show('Do you think the Lord Jesus did not know the trouble');""")
        self.assertTrue(self.js.eval("c.asked"))
        self.js.eval("quoteCloseBack(c.was, c.prev, c.el, c.segId, c.shut, 0.22)")
        self.assertEqual(self.js.eval("text(b)"), "“What do you want me to do to you?”")
        self.assertEqual(self.js.eval("text(c)"), "Do you think the Lord Jesus did not know the trouble")
        self.assertIsNone(self.js.eval("qOpen"))
        # the recogniser's closer, three captions on, now has nothing to close
        self.assertEqual(self.js.eval("text(show('and problem of everyone this morning.”'))"),
                         "and problem of everyone this morning.")
        self.assertFalse(self.js.eval("show('A verse from a hymn says:').asked"))

    def test_the_late_closer_moves_back(self):
        # 2026-11-15 morning, Ruth 1:15: the closer landed after his comment
        self.js.eval("""
          show('but Naomi said to Ruth:');
          var b = show('“Look, your sister-in-law has come back.');
          var c = show('Once again, Naomi tests Ruth\\'s faithfulness.”');""")
        self.assertTrue(self.js.eval("c.asked"))
        self.assertEqual(self.js.eval("c.shut"), 0)
        self.js.eval("quoteCloseBack(c.was, c.prev, c.el, c.segId, c.shut, 0.01)")
        self.assertEqual(self.js.eval("text(b)"), "“Look, your sister-in-law has come back.”")
        self.assertEqual(self.js.eval("text(c)"), "Once again, Naomi tests Ruth's faithfulness.")

    def test_in_french_too(self):
        self.js.eval("""
          var b = show('Il dit : « Seigneur, aie pitié de moi', 'fr');
          var c = show('Et puis il ajoute »', 'fr');""")
        self.assertTrue(self.js.eval("c.asked"))
        self.js.eval("quoteCloseBack(c.was, c.prev, c.el, c.segId, c.shut, 0.1)")
        self.assertEqual(self.js.eval("text(b)"), "Il dit : « Seigneur, aie pitié de moi»")
        self.assertEqual(self.js.eval("text(c)"), "Et puis il ajoute")

    def test_an_opener_with_nothing_after_it_is_taken_back(self):
        self.js.eval("""
          var b = show('and he says: “');
          var c = show('we will go through a few passages');""")
        self.js.eval("quoteCloseBack(c.was, c.prev, c.el, c.segId, c.shut, 0.1)")
        self.assertEqual(self.js.eval("text(b)"), "and he says:")

    def test_not_asked_without_an_open_quotation(self):
        self.js.eval("show('“Blessed be the Lord!”')")
        self.assertFalse(self.js.eval("show('Amen.').asked"))
        # a caption that opens a new quotation takes the old one back instead
        self.js.eval("show('“They arrived in Jericho')")
        self.assertFalse(self.js.eval("show('“Jesus, son of David, have mercy on me!”').asked"))


class QuoteCheck(unittest.TestCase):
    LINES = [{"ro": "Mai departe, Domnul Iisus îi spune:", "en": "Further on, the Lord Jesus tells him:"},
             {"ro": "«Ce vrei să-ți fac?", "en": "“What do you want me to do to you?"},
             {"ro": "Crezi că Domnul Iisus nu a știut necazul", "en": "Do you think the Lord Jesus did not know"}]

    def check(self, answers):
        seen = []

        def ask(text, qs, timeout):
            seen.append((text, [q["name"] for q in qs]))
            if isinstance(answers, Exception):
                raise answers
            return answers
        with mock.patch.object(rg, "_ask", ask):
            return rg.quote_check(self.LINES, 1), seen

    def test_the_preachers_own_words_close_it(self):
        out, seen = self.check({"qin": 0.22})
        self.assertEqual((out["act"], out["qin"]), ("close", 0.22))
        text, names = seen[0]
        self.assertEqual(names, ["qin"])
        self.assertEqual([l.split("]")[0] for l in text.splitlines()], ["[earlier", "[QUOTE OPENS", "[NEWEST"])

    def test_still_quoted_or_no_answer_leaves_it(self):
        self.assertEqual(self.check({"qin": 0.9})[0]["act"], "")
        self.assertEqual(self.check({})[0]["act"], "")
        out = self.check(TimeoutError())[0]
        self.assertEqual((out["act"], out["qin"]), ("", None))


class Pronoun(unittest.TestCase):
    PASS = {"v": 0.1, "sens": 0.0, "sl": 0.1}

    def decide(self, en, answers, div=0.5, ro="ca să-și reverse harul peste noi toți", src="ro", tgt="en"):
        seen = []

        def ask(text, qs, timeout):
            seen.append(([q["name"] for q in qs], qs, text))
            return answers
        with mock.patch.object(rg, "_ask", ask):
            return rg.decide(ro, en, [], timeout=1.0, th=0.3, div=div, src=src, tgt=tgt), seen

    def names(self, seen):
        return [n for names, _, _ in seen for n in names]

    def test_a_lower_case_pronoun_for_god_goes_on(self):
        out, seen = self.decide("that he may pour out his grace on all of us", dict(self.PASS, div=0.87))
        self.assertEqual((out["route"], out["why"], out["div"]), (True, "pronoun", 0.87))
        self.assertIn(["v", "sens", "div"], [names for names, _, _ in seen])

    def test_under_the_bar_or_already_capitalised_stays(self):
        self.assertFalse(self.decide("that he may pour out his grace", dict(self.PASS, div=0.4))[0]["route"])
        # no lower-case pronoun to fix, whatever the score
        self.assertFalse(self.decide("that He may pour out His grace", dict(self.PASS, div=0.9))[0]["route"])
        # "lui" before a name is the genitive article, not a pronoun
        ro = dict(src="en", tgt="ro", ro="For the love of Christ compels us")
        self.assertFalse(self.decide("„Căci iubirea lui Hristos ne stăpânește", dict(self.PASS, div=0.9),
                                     **ro)[0]["route"])
        self.assertTrue(self.decide("Glorificat fie numele lui.", dict(self.PASS, div=0.9), **ro)[0]["route"])
        # each language's own forms: Polish
        self.assertTrue(self.decide("dziękujemy ci za twoją łaskę", dict(self.PASS, div=0.9), tgt="pl")[0]["route"])

    def test_a_doubt_above_it_names_the_reason(self):
        out = self.decide("that he may pour out his grace", {"v": 0.1, "sens": 0.0, "sl": 0.8, "div": 0.9})[0]
        self.assertEqual(out["why"], "misheard")

    def test_not_asked_where_captions_keep_lower_case(self):
        for tgt in ("fr", "de", "es", None):
            out, seen = self.decide("il a répandu sa grâce", dict(self.PASS, div=0.9), tgt=tgt)
            self.assertNotIn("div", self.names(seen), tgt)
            self.assertFalse(out["route"], tgt)

    def test_a_church_can_change_the_list(self):
        rg.questions.cache_clear()
        try:
            with mock.patch.dict(os.environ, {"CT_DIVINE_CAPS": "en,es"}):
                out, seen = self.decide("él derramó su gracia", dict(self.PASS, div=0.9), tgt="es")
                self.assertIn("div", self.names(seen))
                self.assertTrue(out["route"])          # no forms listed for Spanish: Luna's word alone
                self.assertNotIn("div", self.names(self.decide("him", dict(self.PASS, div=0.9), tgt="ro")[1]))
        finally:
            rg.questions.cache_clear()

    def test_off_does_not_ask(self):
        out, seen = self.decide("that he may pour out his grace", dict(self.PASS, div=0.9), div=0.0)
        self.assertEqual((out["route"], "div" in out), (False, False))
        self.assertNotIn("div", self.names(seen))

    def test_the_questions_name_the_languages(self):
        _, seen = self.decide("Que Dios derrame su gracia", self.PASS, src="en", tgt="es",
                              ro="May God pour out his grace")
        text = " ".join(q["instructions"] + json.dumps(q.get("choices", "")) + t
                        for _, qs, t in seen for q in qs)
        self.assertIn("English sermon", text)
        self.assertIn("into Spanish", text)
        self.assertIn("Newest segment, Spanish: Que Dios", text)
        self.assertNotIn("Romanian sermon", text)
        _, seen = self.decide("x", self.PASS, src=None, tgt=None)
        self.assertIn("source-language sermon", seen[0][1][0]["instructions"])


class DivineNote(unittest.TestCase):
    """The translator's prompt names each language that capitalises God's pronouns."""

    def note(self, env=None):
        import translate_claude as tc
        with mock.patch.dict(os.environ, {} if env is None else {"CT_DIVINE_CAPS": env}):
            return tc.divine_note()

    def test_the_list(self):
        n = self.note()
        for name in ("English (He, Him", "Romanian (El, Îl", "Polish (On", "Russian (Он", "Indonesian (Dia"):
            self.assertIn(name, n)
        self.assertIn("Every other language writes them in lower case, always", n)
        self.assertNotIn("Spanish", n)

    def test_a_church_can_change_it(self):
        self.assertIn("Spanish and Romanian (El", self.note("es,ro"))
        self.assertIn("lower case in every language", self.note("none"))


@need_flask
class Endpoints(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = load_server()
        cls.c = cls.server.app.test_client()

    def test_route_passes_both_languages(self):
        got = []
        srv = self.server

        def decide(ro, en, ctx, **kw):
            got.append((kw["div"], kw["src"], kw["tgt"]))
            return {"route": False, "why": "", "ms": 0}
        with mock.patch.object(srv, "HAS_OPENAI_KEY", True), \
                mock.patch.object(srv.router_gate, "decide", decide):
            self.c.post("/route", json={"ro": "x", "en": "y", "ctx": [], "lang": "es", "src_lang": "en"})
            self.c.post("/route", json={"ro": "x", "en": "y", "ctx": []})
        self.assertEqual(got, [(0.5, "en", "es"), (0.5, None, None)])

    def test_without_a_key_the_judge_is_off(self):
        caps = self.c.get("/caps").get_json()
        self.assertEqual((caps["quote_judge"], caps["router_pronoun"]), ("off", 0.5))
        out = self.c.post("/quote-check", json={"lines": QuoteCheck.LINES, "at": 1}).get_json()
        self.assertEqual(out, {"act": ""})


if __name__ == "__main__":
    unittest.main()
