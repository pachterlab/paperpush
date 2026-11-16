"""Regression tests for prose.acronyms and prose.repeated_words false alarms found in a hand audit of
ICLR 2026 camera-ready papers (validated as bare PDFs). Each test mirrors one pattern with short
synthetic text; the true positives the audit confirmed must keep firing."""
from paperpush import style_check as sc
from paperpush.prose_check import repeated_words


def _names(hits):
    return [h.split("'")[1] for h in hits]


# --- acronyms: definitions the check missed ---------------------------------------------------


def test_definition_in_an_introduction_run_into_the_abstract_is_main_text():
    # The abstract read from the PDF runs on into the introduction, where the acronym is defined.
    abstract_text = ("We study safe reinforcement learning with adversarial losses and constraints in linear settings. "
                     "We give a primal-dual algorithm. 1 INTRODUCTION Linear constrained Markov decision processes (CMDPs) model safety.")
    paragraphs = [
        "A Title Jane Doe ABSTRACT We study safe reinforcement learning with adversarial losses and constraints in linear settings. "
        "We give a primal-dual algorithm. 1 INTRODUCTION Linear constrained Markov decision processes (CMDPs) model safety. "
        "The CMDP setting is hard.",
        "Our CMDP algorithm achieves sublinear regret. A CMDP has a cost function.",
    ]
    abstract, body = sc.split_abstract(paragraphs, abstract_text)
    assert abstract and "INTRODUCTION" not in abstract[-1]
    assert body[0].startswith("1 INTRODUCTION")
    found = sc.acronyms(body, abstract)
    assert found.undefined == [] and found.before_definition == [] and found.abstract_only == [] and found.conflicting == []
    assert sc.abstract_text_end(abstract_text).rstrip().endswith("algorithm.")


def test_plural_definitions_are_one_meaning():
    found = sc.acronyms(["We use constrained Markov decision processes (CMDPs) here. The CMDPs are linear.",
                         "Every constrained Markov decision process (CMDP) has a cost. The CMDP ran."])
    assert found.conflicting == []
    assert sc._singular("processes") == "process" and sc._singular("loss") == "loss" and sc._singular("networks") == "network"


def test_definition_with_a_citation_inside_the_parentheses():
    found = sc.acronyms(["Advantages use Generalized Advantage Estimation (GAE; Schulman et al., 2018) here.",
                         "The GAE estimate is smooth. The GAE lambda is 0.95."])
    assert found.undefined == [] and found.before_definition == []


def test_pdf_legend_definition_is_not_reported_as_legend_only():
    # A PDF's legend boundary is a guess: the intro text after a legend lands in ``floats``.
    floats = ["Figure 1: Overview. We integrate both first-order (FO) and zeroth-order (ZO) optimization."]
    body = ["Our method costs no more memory than the FO counterpart.", "We leverage ZO optimization for the base model. FO and ZO both help."]
    assert _names(sc.acronyms(body, floats=floats).legend_only) == ["FO", "ZO"]  # LaTeX/Word legends are exact
    found = sc.acronyms(body, floats=floats, legends_reliable=False)
    assert found.legend_only == [] and found.undefined == []


def test_acronym_introduced_in_words_is_defined():
    found = sc.acronyms(["We apply data augmentation, abbreviated as DA. For implementation, DA uses flips.", "Then DA again."])
    assert found.undefined == []


# --- acronyms: math, split names, proper names --------------------------------------------------


def test_letters_of_a_flattened_formula_are_not_acronyms():
    body = [
        "The value is written as V(s) = EP,π[PH j=h ℓj(sj,aj)| sh = s]. We also have EP,π[x] here.",
        "Regret is defined as Regret(K) = PK k=1(V(s1) - V(s1)) and the bound is O(d3HK +d2|A|HK 2), with PH j=1 x.",
        "Then we have KX k=1 Yk(b) = 0, and KX k=1 Zk = 1.",
        "The evidence lower bound (ELBO)LELBO is maximized, where ELBO(η; x) =:LSV AE(η; x) and LELBO is (24).",
        "Hence E[δt]>0 =⇒E[A GAE t ]>0 =⇒b i >0. Also KL(p||q) and KL(q||p) and VQ(z;E) and VQ(z;F).",
    ]
    found = sc.acronyms(body)
    assert found.undefined == [] and found.before_definition == []


def test_fragments_of_a_split_name_are_not_acronyms():
    body = [
        "We define the sparse variational autoencoder (SVAE). SV AE builds on a V AE and SV AE is fast.",
        "Limitations aside, we propose AUTO - RT, a framework. AUTO - RT finds attacks and A UTO - RT is fast.",
    ]
    found = sc.acronyms(body)
    assert found.undefined == [] and found.before_definition == []


def test_cited_model_and_benchmark_names_are_not_undefined():
    body = [
        "We test on benchmarks including GQA (Hudson & Manning, 2019), MME (Fu et al., 2023), and MMMU (Yue et al., 2024).",
        "GQA and MME and MMMU scores rise. We train on the CIRR dataset and the CIRR images.",
        "A vision encoder (e.g., CLIP, SigLIP) and CLIP features; GPT outputs and CIFAR images; GPT again on CIFAR.",
        "In molecular generation, EBD (Park & Shen, 2024) blurs. We also evaluate Equivariant Blurring Diffusion (EBD) here.",
    ]
    found = sc.acronyms(body)
    assert found.undefined == [] and found.before_definition == []


def test_terms_every_ml_reader_knows_and_small_caps_headings():
    body = [
        "Reducing FLOPs. Cost is measured in floating-point operations (FLOPs) and FLOPs matter.",
        "We frame the task as an MDP and train with PPO. The MDP has PPO updates on an LLM and a GPU.",
        "3 METHOD In this section we describe our method. The METHOD heading above is small caps.",
    ]
    found = sc.acronyms(body)
    assert found.undefined == [] and found.before_definition == []


def test_numbers_heavy_figure_text_is_not_a_first_use():
    figure = "2.2 2.4 2.6 2.8 3.0 3.2 3.4 Loss TREC predicts best LR drops 0.00 0.25 0.50 0.75 1.00 0.0 0.2 0.4 0.6 0.8 1.0 Normalized TREC 111M 266M"
    found = sc.acronyms([figure, "We introduce the training re-evaluation curve (TREC). The TREC is flat."])
    assert found.before_definition == []


def test_pdf_tokens_seen_only_in_tables_are_not_undefined():
    body = ["We report results below.", "Method SQA VG ALG GEO", "Ours 1 2 3 4", "Base SQA VG ALG GEO"]
    assert sc.acronyms(body, legends_reliable=False).undefined == []


def test_acknowledgments_are_not_read():
    body = ["We trained a model."] * 4 + ["We are done. ACKNOWLEDGMENTS JQZ and TLG acknowledge NSERC. TLG and HX thank NSERC too."]
    assert sc._before_acknowledgments(body)[-1] == "We are done. "
    assert sc.acronyms(sc._before_acknowledgments(body)).undefined == []


# --- acronyms: true positives that must still fire ------------------------------------------------


def test_use_before_definition_still_fires():
    # OswqOlTYR2: "SDE-based" is used a few lines before "Stochastic Differential Equation (SDE)".
    body = ["The flow transports x t towards x 0. SDE-based iterative denoising is used once trained.",
            "To explore, we adopt a Stochastic Differential Equation (SDE) formulation. The SDE adds noise."]
    assert _names(sc.acronyms(body).before_definition) == ["SDE"]


def test_genuinely_undefined_acronym_still_fires():
    body = ["We compare against the XQZ approach in all runs.", "The XQZ approach is slower than ours in every run."]
    assert _names(sc.acronyms(body, legends_reliable=False).undefined) == ["XQZ"]


def test_abstract_undefined_still_fires_on_a_real_abstract():
    found = sc.acronyms(["A generative adversarial network (GAN) helped. The GAN ran."], ["We used a GAN on the data."])
    assert found.abstract_undefined == ["'GAN'"]


# --- repeated words ------------------------------------------------------------------------------


def test_algorithm_block_terminators_are_not_repeats():
    assert repeated_words("bH ← bH + ∆H. end end M← 0", pdf=True) == []
    assert repeated_words("then Add j into O and V end end end end Append any remaining", pdf=True) == []
    assert repeated_words("; end end xt←xt,n; Output: Final model", pdf=True) == []


def test_table_headers_glued_to_symbols_are_not_repeats():
    assert repeated_words("respectively. en⇒zh zh⇒th Accuracy Naturalness", pdf=True) == []


def test_legend_labels_in_parentheses_are_not_repeats():
    assert repeated_words("Both Missing MoRA (both both) DCP (both both) Figure 3:", pdf=True) == []
    assert repeated_words("MoRA (both image) DCP (image image) 10% 30% 50%", pdf=True) == []


def test_real_repeated_words_still_fire():
    assert len(repeated_words("we compared P-VLMs of different different sizes (base and large).", pdf=True)) == 1
    assert len(repeated_words("It also requires the the exploration of different prompting techniques.", pdf=True)) == 1
    assert len(repeated_words("It also requires the the exploration of different prompting techniques.")) == 1


def test_acronym_followed_by_its_full_term_in_parentheses_is_defined():
    found = sc.acronyms(["Firms make climate investments and ESG (Environmental, Social, and Governance) disclosures.",
                         "Investors weigh ESG scores. Strong ESG incentives change little."])
    assert found.undefined == [] and found.before_definition == [] and found.rarely_used == []
    # A parenthetical whose words do not spell the acronym is not a definition.
    assert _names(sc.acronyms(["The XQZ (see below) is used.", "The XQZ again in every run."]).undefined) == ["XQZ"]
