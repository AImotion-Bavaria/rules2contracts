# highD recordings (not included)

The highD dataset (R. Krajewski, J. Bock, L. Kloeker, L. Eckstein: *The highD
Dataset*, ITSC 2018) is licensed for non-commercial research and must not be
redistributed. Request it at https://levelxdata.com/highd-dataset/ and unpack
the 60 recordings here as

    highd/data/01_recordingMeta.csv  01_tracksMeta.csv  01_tracks.csv  01_highway.png
    highd/data/02_…

(the layout of the official download). Then:

    # R_G1, all 60 recordings, 5 following pairs per recording (300 episodes, 1 200 runs)
    python3 run_rq4.py highd/data/01 highd/data/02 … highd/data/60 --dataset highd \
        --rule contracts/R_G1.yaml --max-pairs 5 --out out/rq4_highd_g1.csv
    python3 diff_csv.py data/rq4_highd_g1_2026-08-17_results.csv out/rq4_highd_g1.csv scenario ego lead mode localisation

    # R_G3, speed-limit rule: the 10 fastest car tracks of each recording with a posted limit
    python3 run_rq4.py highd/data/01 … highd/data/60 --dataset highd \
        --rule contracts/R_G3.yaml --out out/rq4_highd_g3.csv
    python3 diff_csv.py data/rq4_highd_g3_2026-08-17_results.csv out/rq4_highd_g3.csv scenario ego lead mode localisation

`run_rq4.py --dataset highd` takes recording *prefixes* (`highd/data/NN`), as
`highd_traces.py` documents. Recordings without a posted speed limit are
skipped for R_G3 (19 of 60). The canonical result tables produced this way
are in `data/rq4_highd_g*_2026-08-17_results.csv`; the per-episode rows there
(recording, ego id, lead id, frame range) are the episode index.
