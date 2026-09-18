# HTB Rank Planner

U wanna pull your live HTB rank state and calculate what's left for u to reach your next rank ?<br>
This script does exactly that.<br>
It will give u the fastest path, the easiest path, and a hybrid way.<br>
And it takes into account both the active machines and the challenges and differentiate the one you completed and the one u didn't.

```
git clone https://github.com/ascheriit-dkp/HTB-Rank-Planner.git
cd HTB-Rank-Planner
python3 -m pip install -r requirements.txt
# Go into ur browser in ur HTB Labs tab, open the settings > scroll down > API token > create one > copy it.
printf '%s' 'YOUR_HTB_TOKEN' > ~/.htb-token
chmod 600 ~/.htb-token
python3 htb_rank_planner.py --token-file ~/.htb-token --show-progress
```

I would recommend not sharing your token to anyone btw.<br>
If you need more insight about the functionalities of the tool :

```
python3 htb_rank_planner.py --help
```

Of course this script isn't official.<br>
And it use the v4 API of HTB documented by this great guy [Kris Stanley (Propolisa)](https://github.com/Propolisa/htb-api-docs).<br>
Full details of this tool are here : [FULL_DETAILS.md](https://github.com/ascheriit-dkp/HTB-Rank-Planner/blob/docs/FULL_DETAILS.md).
