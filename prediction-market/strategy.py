"""Prediction Market v3 as a frame with arms (study santiago/pm-skeleton-20261117).

The frame reads the seed's spread on its first step and builds that spread's player from the
table ARMS; each step a player reads the state, its core and its arms propose orders, and the
frame's assembly passes them through the hooks and the guard and books what was sent. An arm is
a named unit with its contract in its docstring: what it reads, what it may change, what it costs.
ARMS.md beside this file gives each arm's area of points, its measured size and its dependencies.
This version keeps the v3 frame, with spread-specific fast paths and calibrated
inventory sizing at the penny crossing and for midband bids.
"""
import math
import re
from bisect import bisect_left
from bisect import bisect_right as _bisect_right
from itertools import accumulate
from operator import itemgetter as _itemgetter
from orderbook_pm_challenge.types import CancelAll, CancelOrder, PlaceOrder, Side


# =================================== Switches ===================================
# Each arm plays on the spreads it names; () switches it off. A reading of the spread above 4
# plays as 4. These are v3's choices.
ARMS = {
    # the spread-1 player
    's1_flow': (1,),               # the learned retail flow (rate and size) at spread 1
    's1_stock': (1,),              # YES bought near the touch below 0.10, to cover asks
    's1_value_split': (1,),        # each new ask split between its tick and the next by value
    's1_probes': (1,),             # 0.01 probes between the touches, which the sensor reads
    's1_jump_warning': (1,),       # drops orders on the wrong side of a jump for four steps
    # the native player, spreads 2-4
    'flow_learning': (2, 3, 4),    # the learned retail rate and size
    'probes': (2, 3, 4),           # 0.01 probes around the belief's mean
    'yes_stock': (2, 3, 4),        # one-tick YES bids that cover asks at low prices
    'floor_score_risk': (2,),     # latent-score risk for confirmed, long sub-penny episodes
    'ceiling_score_risk': (2,),   # mirrored forecast for late 99c-censored bids
    'recalibration': (3, 4),       # the learned net that reshapes the belief for pricing
    'sell_controller': (3, 4),     # the myopic two-level ask book while the mean is in 0.10-0.90
    # hooks: measured areas with no code yet
    'queue_place': (1, 2, 3, 4),
    'midband_retail': (1, 2, 3, 4),
    'low_price_capital': (1, 2, 3, 4),
}
# The frame's settings, not arms. GUARD: the spreads whose orders the guard fits to collateral
# and books in the ask ledger (the sell controller needs it). RESET: the spreads where a step's
# exception cancels every order and restarts the player instead of failing the seed.
# FLOW_COMPOUND: the spreads where flow_learning models compound retail orders.
GUARD = (1, 3, 4)
RESET = (1, 3, 4)
FLOW_COMPOUND = (2,)


# ==================================== Frame =====================================
_BUY=Side.BUY;_SELL=Side.SELL
_second=_itemgetter(1)

def _arm(name,spread):
    return spread in ARMS[name]

class Strategy:
    """The frame. The one reading of the spread, on the first step: it picks the player and the
    arms that play, and loads the native tables when the native player plays."""
    def __init__(self):self.player=None
    def on_step(self,state):
        if self.player is None:
            b=state.competitor_best_bid_ticks or 0;a=state.competitor_best_ask_ticks or 100
            spread=(a-b+1)//2
            if spread!=1:_load_tables()
            self.player=S1Player() if spread==1 else NativePlayer(min(4,spread))
        return self.player.on_step(state)

def _hooks(spread):
    return [hook for name,hook in HOOKS if _arm(name,spread)]

def _s1_commit_probes(state,actions,ledger):
    """Exact fast path for cancelling everything and placing tiny probes."""
    orders=state.own_orders
    n=len(orders)
    if len(actions)<n or state.free_cash<.02*(len(actions)+1):
        return False
    named=ledger.named
    available=max(0.,state.yes_inventory-sum(c for t,q,c in named.values()))
    for i,o in enumerate(orders):
        c=actions[i]
        if type(c) is not CancelOrder or c.order_id!=o.order_id:
            return False
        if o.side==_SELL:available+=named[c.order_id][2]
    anonymous=[];ticks=set();full=True
    for o in actions[n:]:
        if (type(o) is not PlaceOrder or o.quantity!=.01 or type(o.quantity) is not float
                or not 1<=o.price_ticks<=99 or type(o.price_ticks) is not int
                or o.client_order_id is not None):
            return False
        if o.side==_SELL:
            tick=o.price_ticks
            if tick in ticks:return False
            ticks.add(tick)
            covered=min(.01,available)
            available-=covered
            anonymous.append((state.step,tick,.01,covered))
            if covered!=.01:full=False
        elif o.side!=_BUY:return False
    ledger.active.clear()
    ledger.named={}
    ledger.anonymous=anonymous
    ledger.full_cover=full
    return True

class S1Player:
    """Spread-one player with guarded collateral and restart-safe IDs."""
    def __init__(self):
        self.guarded=1 in GUARD
        self.reset=1 in RESET
        self.hooks=_hooks(1)
        self._start()
    def _start(self):
        self.core=S1Core()
        self.ledger=SentAskLedger() if self.guarded else None
    def on_step(self,state):
        if not self.reset:return self.play_step(state)
        try:
            return self.play_step(state)
        except Exception:
            # Unexpected history never escapes as a failed seed. Clear actual
            # orders and restart private beliefs/ledger, preserving unique ids.
            serial=self.core.serial
            self._start()
            self.core.serial=serial
            return [CancelAll()]
    def play_step(self,state):
        ledger=self.ledger
        if ledger is not None:self.core.uncovered=ledger.observe(state)
        actions=self.core.on_step(state)
        for hook in self.hooks:actions=hook(self,state,actions)
        if ledger is not None and (actions or not ledger.full_cover):
            if self.core.probe_only_step!=state.step or not _s1_commit_probes(state,actions,ledger):
                actions=s1_guard_actions(state,actions,ledger,commit=True)
        return actions

class NativePlayer:
    """Spreads 2-4. Each step: the ask ledger reads the state, the order memory keeps last step's
    copy, flow_learning sets the retail model, the core proposes (with probes, yes_stock and
    recalibration inside it), the sell controller replaces the core's asks while the mean is in
    0.10-0.90, the hooks run, the guard fits every order to collateral, and the order memory
    books what was sent."""
    def __init__(self,spread):
        self.spread=spread
        self.guarded=spread in GUARD
        self.reset=spread in RESET
        self.hooks=_hooks(spread)
        if spread==2 and self.hooks==[queue_place,midband_retail,low_price_capital]:self.hooks=()
        self._start()
    def _start(self):
        spread=self.spread
        self.mem=FastOrderMemory() if spread==4 else OrderMemory()
        self.fast=spread==2 and not self.guarded and not self.hooks
        flow_type=FastFlowLearner if spread==3 and spread not in FLOW_COMPOUND else FlowLearner
        self.flow=flow_type(spread in FLOW_COMPOUND) if _arm('flow_learning',spread) else None
        if self.fast and self.flow is not None:self.flow.fast=True
        if spread==4 and self.flow is not None:self.flow._zerocache={}
        controlled=_arm('sell_controller',spread) and self.guarded
        self.core=NativeCore(self.mem,
            NativeProbes() if _arm('probes',spread) else None,
            YesStock(spread==3) if _arm('yes_stock',spread) else None,
            Recalibration() if _arm('recalibration',spread) else None,
            controlled)
        if spread==3:self.core.inside_probe_stop=0.055
        if spread==4:self.core.s4_stop_calibrate=True
        if self.fast:self.core.fast=True
        self.core.floor_risk=FloorScoreRisk() if _arm('floor_score_risk',spread) else None
        self.core.ceiling_risk=FloorScoreRisk() if _arm('ceiling_score_risk',spread) else None
        self.controller=SellController(self.core) if controlled else None
        if self.controller is not None:self.controller.fast=(spread==4)
        self.ledger=SentAskLedger() if self.guarded else None
    def on_step(self,state):
        if not self.reset:return self.play_step(state)
        try:
            return self.play_step(state)
        except Exception:
            # Public-history/numerical safety fallback, never intentional failure.
            # Preserve explicit namespaces' serials so reset cannot reuse IDs.
            serial=self.core.serial
            controller=self.controller
            self._start()
            self.core.serial=serial
            if controller is not None:self.controller.optimizer.serial=controller.optimizer.serial
            return [CancelAll()]
    def play_step(self,state):
        ledger=self.ledger;mem=self.mem;core=self.core
        if ledger is not None:ledger.observe(state,False)
        mem.prior_prevq=mem.prevq
        mem.prior_front=mem.front
        if self.flow is not None:
            if self.spread==4 and ledger is not None:self.flow.update(core,mem,state,ledger.active)
            else:self.flow.update(core,mem,state)
        b=state.competitor_best_bid_ticks or 0;a=state.competitor_best_ask_ticks or 100
        actions=core.propose(state,b,a)
        if self.controller is not None and .10<=core._belmean/100.<=.90:
            if self.controller.fast:
                actions,self.controller.optimizer.serial=_fast_sell4(state,actions,core,mem,ledger,self.controller.optimizer.serial)
            else:actions=self.controller.step(state,actions,core,mem,ledger.named)
        for hook in self.hooks:actions=hook(self,state,actions)
        if ledger is not None:
            if self.spread==4:
                actions=guard_spread4(state,actions,ledger)
            else:
                guarded=native_guard_fast(state,actions,ledger) if self.spread==3 else None
                actions=s1_guard_actions(state,actions,ledger,commit=True,capture=True) if guarded is None else guarded
            mem.commit_actions(state,actions,ledger.guard_retained,ledger.guard_new)
        elif self.fast:
            self.core.fast_commit(state,b,a,actions)
        else:mem.commit_actions(state,actions)
        return actions

class OrderMemory:
    """The native player's memory of its own orders, which the core, flow_learning and the sell
    controller read and only commit_actions writes, from the actions actually sent: which orders
    hold queue place (front), each order's last quantity (prevq), the quantity resting at each
    tick (prevbuy, prevask), the meaningful orders (lastmain), the certified orders whose fills
    measure retail (flowlast, pfront, pb, pa), and last step's copies (prior_prevq, prior_front)."""
    def __init__(self):
        self.prevbuy=np.zeros(101);self.prevask=np.zeros(101)
        self.front=set();self.prevq={};self.lastmain=[]
        self.flowlast={};self.pfront=set();self.pb=0;self.pa=100
        self.prior_prevq={};self.prior_front=set()
    def commit_actions(self,state,actions,resolved=None,fresh=None):
        if resolved is None:
            retained={o.order_id:(o.side,o.price_ticks,o.remaining_quantity) for o in state.own_orders}
            new=[]
            for action in actions:
                if isinstance(action,CancelAll):
                    retained.clear();new=[]
                elif isinstance(action,CancelOrder):
                    retained.pop(action.order_id,None)
                    if new:new=[o for o in new if o.client_order_id!=action.order_id]
                elif isinstance(action,PlaceOrder):
                    new.append(action)
            resolved=retained.items()
        else:new=fresh
        b=state.competitor_best_bid_ticks or 0
        a=state.competitor_best_ask_ticks or 100
        BUY=_BUY
        front=set();prevq={};lastmain=[];last={}
        priorq=self.prior_prevq;priorfront=self.prior_front
        pb=self.prevbuy;pa=self.prevask
        pb[:]=0.;pa[:]=0.
        for oid,(side,tick,q) in resolved:
            if q<=.011 or oid in priorfront or (tick>b if side==BUY else tick<a) or q<priorq.get(oid,q)-.005:front.add(oid)
            prevq[oid]=q
            if side==BUY:pb[tick]+=q
            else:pa[tick]+=q
            if q>.011:lastmain.append((oid,side,tick,q))
            if q>=.1:last[oid]=(side,tick,q)
        for order in new:
            oid=order.client_order_id;side=order.side;tick=order.price_ticks;q=order.quantity
            if oid is not None:
                if not oid.startswith('h') and (tick>b if side==BUY else tick<a):front.add(oid)
                prevq[oid]=q
            if side==BUY:pb[tick]+=q
            else:pa[tick]+=q
            if q>.011:lastmain.append((oid,side,tick,q))
            if oid is not None and q>=.1:last[oid]=(side,tick,q)
        self.front=front;self.prevq=prevq;self.lastmain=lastmain
        bestb=None;nb=0;besta=None;na=0
        for v in last.values():
            if v[0]==BUY:
                if bestb is None or v[1]>bestb:bestb=v[1];nb=1
                elif v[1]==bestb:nb+=1
            else:
                if besta is None or v[1]<besta:besta=v[1];na=1
                elif v[1]==besta:na+=1
        self.flowlast={oid:v for oid,v in last.items() if ((v[1]==bestb and nb==1) if v[0]==BUY else (v[1]==besta and na==1))}
        self.pfront=front
        self.pb=b;self.pa=a

"""The ask ledger: public StepState + actually sent actions. Assumes the fresh strategy begins
with no asks and at most one anonymous SELL per price/step (as the probes satisfy). No hidden
simulator data is read by this component.
"""

class FastOrderMemory(OrderMemory):
    def commit_actions(self,state,actions,resolved=None,fresh=None):
        if resolved is None:return super().commit_actions(state,actions,resolved,fresh)
        b=state.competitor_best_bid_ticks or 0;a=state.competitor_best_ask_ticks or 100
        front=set();prevq={};lastmain=[]
        priorq=self.prior_prevq;priorfront=self.prior_front
        pb=self.prevbuy;pa=self.prevask
        pb.fill(0.);pa.fill(0.)
        bestb=0;besta=100;ob=None;oa=None;seq=0
        for oid,(side,tick,q) in resolved:
            if q<=.011 or oid in priorfront or (tick>b if side is _BUY else tick<a) or q<priorq.get(oid,q)-.005:front.add(oid)
            prevq[oid]=q
            if side is _BUY:pb[tick]+=q
            else:pa[tick]+=q
            if q>.011:
                lastmain.append((oid,side,tick,q))
                if q>=.1:
                    if side is _BUY:
                        seq+=1
                        if tick>bestb:bestb=tick;ob=(oid,(side,tick,q),seq)
                        elif tick==bestb:ob=None
                    else:
                        seq+=1
                        if tick<besta:besta=tick;oa=(oid,(side,tick,q),seq)
                        elif tick==besta:oa=None
        for order in fresh:
            oid=order.client_order_id;side=order.side;tick=order.price_ticks;q=order.quantity
            if oid is not None:
                if (tick>b if side is _BUY else tick<a) and not oid.startswith('h'):front.add(oid)
                prevq[oid]=q
            if side is _BUY:pb[tick]+=q
            else:pa[tick]+=q
            if q>.011:
                lastmain.append((oid,side,tick,q))
                if q>=.1 and oid is not None:
                    if side is _BUY:
                        seq+=1
                        if tick>bestb:bestb=tick;ob=(oid,(side,tick,q),seq)
                        elif tick==bestb:ob=None
                    else:
                        seq+=1
                        if tick<besta:besta=tick;oa=(oid,(side,tick,q),seq)
                        elif tick==besta:oa=None
        self.front=front;self.prevq=prevq;self.lastmain=lastmain
        last={}
        if ob is not None and (oa is None or ob[2]<oa[2]):last[ob[0]]=ob[1]
        if oa is not None:last[oa[0]]=oa[1]
        if ob is not None and ob[0] not in last:last[ob[0]]=ob[1]
        self.flowlast=last;self.pfront=front;self.pb=b;self.pa=a

class SentAskLedger:
    def __init__(self):
        self.named={}
        self.anonymous=[]
        self.full_cover=True
    @staticmethod
    def cents(q):
        return round(math.floor((q+1e-9)/.01)*.01,10)
    def observe(self,state,want=True):
        self.seen=state
        if self.full_cover:
            updated={};self.active={}
            for o in state.own_orders:
                self.active[o.order_id]=(o.side,o.price_ticks,o.remaining_quantity)
                if o.side==_SELL:updated[o.order_id]=(o.price_ticks,o.remaining_quantity,o.remaining_quantity)
            self.named=updated;self.anonymous=[]
            if want:return {oid:0. for oid in updated}
            return
        live={};self.active={}
        for o in state.own_orders:
            self.active[o.order_id]=(o.side,o.price_ticks,o.remaining_quantity)
            if o.side==_SELL:live[o.order_id]=o
        # Resolve previous anonymous asks from ORIGINAL views, never synthesized
        # or repaired candidate inputs. A fully filled anonymous probe disappears.
        unbound=[o for oid,o in live.items() if oid not in self.named] if self.anonymous else []
        for step,tick,quantity,covered in self.anonymous:
            matches=[o for o in unbound if o.submitted_step==step and o.price_ticks==tick and o.remaining_quantity<=quantity+1e-8]
            if not matches:continue
            assert len(matches)==1,'anonymous SELL identity ambiguous'
            order=matches[0];unbound.remove(order)
            self.named[order.order_id]=(tick,quantity,covered)
        self.anonymous=[]
        updated={}
        for oid,order in live.items():
            tick,oldquantity,oldcover=self.named[oid]
            assert tick==order.price_ticks
            quantity=order.remaining_quantity
            if quantity==oldquantity:
                covered=round(oldcover,10)
            else:
                filled=round(oldquantity-quantity,10)
                assert filled>=-1e-8
                covered=round(max(0.,oldcover-max(0.,filled)),10)
            assert covered<=quantity+1e-8
            updated[oid]=(tick,quantity,covered)
        self.named=updated
        if want:return {oid:round(q-cover,10) for oid,(tick,q,cover) in updated.items()}

"""The guard: actual-sent ask cover and final original-action collateral check.
Uses only published StepState and own actually returned actions, no hidden state.
"""
_PROBE_BID_COST=[round(t/100.*.01,10) for t in range(101)]
def s1_guard_actions(state,actions,ask_ledger,_isfinite=math.isfinite,commit=False,capture=False):
    BUY=_BUY;SELL=_SELL;cents=SentAskLedger.cents
    active=ask_ledger.active
    cover={oid:c for oid,(tick,q,c) in ask_ledger.named.items()}
    available=max(0.,state.yes_inventory-sum(cover.values()))
    free=state.free_cash
    emitted=[];anonymous_sells=set();anonymous_records=[]
    captured=None;fresh_actions=[];complex_actions=False
    for action in actions:
        if isinstance(action,CancelAll):
            if captured is not None:complex_actions=True
            active.clear();cover.clear();anonymous_sells.clear();anonymous_records.clear();free=state.cash;available=state.yes_inventory
            emitted.append(action)
        elif isinstance(action,CancelOrder):
            if captured is not None:complex_actions=True
            order=active.pop(action.order_id,None)
            if order is None:continue
            side,tick,remaining=order
            px=tick/100.
            if side==BUY:
                free+=round(px*remaining,10)
            else:
                cov=cover.pop(action.order_id,0.)
                free+=round((remaining-cov)*(1.-px),10)
                available+=cov
            emitted.append(action)
        elif isinstance(action,PlaceOrder):
            tick=action.price_ticks
            quantity=action.quantity
            if not isinstance(tick,int) or isinstance(tick,bool) or not 1<=tick<=99:continue
            aside=action.side
            if aside not in (BUY,SELL) or not _isfinite(quantity) or quantity<.01:continue
            oid=action.client_order_id
            if oid is not None and (not isinstance(oid,str) or not oid or oid in active):continue
            if oid is None and aside==SELL and tick in anonymous_sells:continue
            px=tick/100.
            isbuy=aside==BUY
            maximum=max(0.,free)/px if isbuy else available+max(0.,free)/(1.-px)
            q=.01 if quantity==.01 and maximum>=.01 else cents(min(quantity,max(0.,maximum)))
            q1=q
            required=_PROBE_BID_COST[tick] if isbuy and q==.01 else (round(px*q,10) if isbuy else (0. if available>=q else round((q-available)*(1.-px),10)))
            if required>free+1e-9:
                # Stay strictly below the engine's epsilon/cent-round boundary.
                q=min(q,cents(max(0.,maximum-1e-8)))
            if q<.01:continue
            if isbuy:
                amount=required if q==q1 else round(px*q,10)
                free-=amount
            else:
                cov=min(q,available)
                amount=required if q==q1 else round((q-cov)*(1.-px),10)
                available-=cov
                free-=amount
                if oid is not None:cover[oid]=cov
                else:anonymous_sells.add(tick)
            if free < -1e-8:
                # Numerical safety: no place is sent if accounting went negative.
                if isbuy:free+=amount
                else:
                    available+=cov;free+=amount
                    if oid is not None:cover.pop(oid,None)
                    else:anonymous_sells.discard(tick)
                continue
            placed=action if (q==quantity and type(quantity) is float) else PlaceOrder(aside,tick,q,oid)
            emitted.append(placed)
            if capture:
                if captured is None:captured=list(active.items())
                fresh_actions.append(placed)
            if oid is not None:active[oid]=(aside,tick,q)
            elif not isbuy:anonymous_records.append((state.step,tick,q,cov))
    if commit:
        ask_ledger.named={oid:(tick,q,cover[oid]) for oid,(side,tick,q) in active.items() if side==SELL}
        ask_ledger.anonymous=anonymous_records
        ask_ledger.full_cover=all(q==c for tick,q,c in ask_ledger.named.values()) and all(q==c for step,tick,q,c in anonymous_records)
    if capture:
        if complex_actions:ask_ledger.guard_retained=None;ask_ledger.guard_new=None
        else:
            ask_ledger.guard_retained=list(active.items()) if captured is None else captured
            ask_ledger.guard_new=fresh_actions
    return emitted



_GUARD_X=[t/100. for t in range(101)]
_GUARD_Y=[1.-x for x in _GUARD_X]
def guard_spread4(state,actions,ledger):
    active=ledger.active;named=ledger.named
    covered={oid:c for oid,(t,q,c) in named.items()}
    available=state.yes_inventory-sum(covered.values())
    if available<0.:available=0.
    free=state.free_cash
    emitted=[];new=[];anonymous=[];ticks=set()
    # Only the normal pattern of cancels followed by placements is optimized.
    # Keep a separate active dictionary so any unusual pattern can safely
    # delegate to the original guard with its original bookkeeping.
    active=active.copy()
    retained=None
    for action in actions:
        typ=type(action)
        if typ is CancelOrder and retained is None:
            oid=action.order_id
            old=active.pop(oid,None)
            if old is not None:
                side,t,q=old
                if side is _BUY:free+=round(_GUARD_X[t]*q,10)
                else:
                    cov=covered.pop(oid,0.)
                    free+=round((q-cov)*_GUARD_Y[t],10)
                    available+=cov
                emitted.append(action)
            continue
        if typ is not PlaceOrder:
            return s1_guard_actions(state,actions,ledger,commit=True,capture=True)
        t=action.price_ticks;q=action.quantity;oid=action.client_order_id
        side=action.side
        if type(t) is not int or not 1<=t<=99 or (side is not _BUY and side is not _SELL) or not math.isfinite(q) or q<.01:
            continue
        if oid is not None:
            if not isinstance(oid,str) or not oid or oid in active:continue
        elif side is _SELL and t in ticks:continue
        isbuy=side is _BUY
        px=_GUARD_X[t] if isbuy else _GUARD_Y[t]
        cap=(free if free>0. else 0.)/px+(0. if isbuy else available)
        if q==.01 and cap>=.01:adj=.01
        else:
            adj=SentAskLedger.cents(min(q,max(0.,cap)))
        if adj<.01:continue
        if isbuy:
            cov=0.
            required=_PROBE_BID_COST[t] if adj==.01 else round(px*adj,10)
        else:
            cov=min(adj,available)
            required=0. if cov>=adj else round((adj-cov)*px,10)
        if required>free+1e-9:
            adjusted=min(adj,SentAskLedger.cents(max(0.,cap-1e-8)))
            if adjusted<.01:continue
            if adjusted!=adj:
                adj=adjusted
                if not isbuy:cov=min(adj,available)
                required=round((adj-cov)*px,10)
        if free-required < -1e-8:continue
        free-=required
        target=action if adj==q and type(q) is float else PlaceOrder(side,t,adj,oid)
        if retained is None:retained=list(active.items())
        if isbuy:
            if oid is not None:active[oid]=(side,t,adj)
        else:
            available-=cov
            if oid is not None:
                active[oid]=(side,t,adj);covered[oid]=cov
            else:
                ticks.add(t)
                anonymous.append((state.step,t,adj,cov))
        emitted.append(target);new.append(target)
    ledger.named={oid:(t,q,covered[oid]) for oid,(side,t,q) in active.items() if side is _SELL}
    ledger.anonymous=anonymous
    ledger.full_cover=all(q==c for t,q,c in ledger.named.values()) and all(q==c for step,t,q,c in anonymous)
    ledger.guard_retained=list(active.items()) if retained is None else retained
    ledger.guard_new=new
    ledger.active=active
    return emitted


def native_guard_fast(state,actions,ledger,_int=int,_finite=math.isfinite,_floor=math.floor):
    """Fast collateral proof for spread-3 actions; defer all repairs to the general guard."""
    active=ledger.active.copy()
    named=ledger.named.copy()
    yes=max(0.,state.yes_inventory-sum(v[2] for v in named.values()))
    cash=state.free_cash
    new=[];newids=set();anon=[];anon_ticks=set()
    fresh=False
    BUY=_BUY;SELL=_SELL
    for ac in actions:
        kind=type(ac)
        if kind is CancelOrder:
            if fresh:return None
            oid=ac.order_id
            order=active.pop(oid,None)
            if order is None:return None
            side,t,q=order
            if side is BUY:cash+=round(q*(t/100.),10)
            else:
                c=named.pop(oid)[2]
                yes+=c;cash+=round((q-c)*(1.-t/100.),10)
        elif kind is PlaceOrder:
            fresh=True
            t=ac.price_ticks;q=ac.quantity;side=ac.side;oid=ac.client_order_id
            if type(t) is not _int or not 1<=t<=99 or type(q) is not float or not q>=.01 or not _finite(q):return None
            if q!=.01 and _floor((q+1e-9)/.01)/100.!=q:return None
            if oid is not None:
                if type(oid) is not str or not oid or oid in active or oid in newids:return None
                newids.add(oid)
            if side is BUY:
                req=_PROBE_BID_COST[t] if q==.01 else round(q*(t/100.),10)
            elif side is SELL:
                c=min(yes,q)
                yes-=c
                req=0. if c==q else round((q-c)*(1.-t/100.),10)
                if oid is not None:named[oid]=(t,q,c)
                else:
                    if t in anon_ticks:return None
                    anon_ticks.add(t);anon.append((state.step,t,q,c))
            else:return None
            # Required cash is rounded the same way as the exchange; a strict
            # margin leaves the old guard responsible for any borderline fit.
            if req>0 and req>cash-1e-7:return None
            cash-=req
            new.append(ac)
        else:return None
    ledger.named=named;ledger.anonymous=anon
    ledger.full_cover=all(q==c for t,q,c in named.values()) and all(q==c for st,t,q,c in anon)
    ledger.guard_retained=active.items();ledger.guard_new=new
    return actions

# ================================ Frame library =================================
# Normal cdf (from math.erf/erfc), its inverse (Wichura's AS241) and the nearest-boundary
# gaussian filter, without scipy.
_SQRTH=0.7071067811865476
def _ndtr1(a):
    x=a*_SQRTH
    z=x if x>=0. else -x
    if z<_SQRTH:return .5+.5*math.erf(x)
    y=.5*math.erfc(z)
    return 1.-y if x>0. else y
def ndtr(a,_e=math.erfc,_s=_SQRTH):
    # One formula for every input, so a value computed alone equals the same value computed
    # inside an array (ties between the two paths must stay ties).
    if isinstance(a,float) and not isinstance(a,np.generic):return .5*_e(-a*_s)
    arr=np.asarray(a,dtype=np.float64)
    if arr.ndim==0:return np.float64(.5*_e(-float(arr)*_s))
    flat=((-arr.ravel())*_s).tolist()
    return (np.array([_e(x) for x in flat])*.5).reshape(arr.shape)
def ndtri(p):
    p=float(p)
    q=p-.5
    if abs(q)<=.425:
        r=.180625-q*q
        num=(((((((2.5090809287301226727e+3*r+3.3430575583588128105e+4)*r+6.7265770927008700853e+4)*r+4.5921953931549871457e+4)*r+1.3731693765509461125e+4)*r+1.9715909503065514427e+3)*r+1.3314166789178437745e+2)*r+3.3871328727963666080e+0)*q
        den=(((((((5.2264952788528545610e+3*r+2.8729085735721942674e+4)*r+3.9307895800092710610e+4)*r+2.1213794301586595867e+4)*r+5.3941960214247511077e+3)*r+6.8718700749205790830e+2)*r+4.2313330701600911252e+1)*r+1.0)
        return num/den
    r=p if q<=0. else 1.-p
    r=math.sqrt(-math.log(r))
    if r<=5.:
        r=r-1.6
        num=(((((((7.74545014278341407640e-4*r+2.27238449892691845833e-2)*r+2.41780725177450611770e-1)*r+1.27045825245236838258e+0)*r+3.64784832476320460504e+0)*r+5.76949722146069140550e+0)*r+4.63033784615654529590e+0)*r+1.42343711074968357734e+0)
        den=(((((((1.05075007164441684324e-9*r+5.47593808499534494600e-4)*r+1.51986665636164571966e-2)*r+1.48103976427480074590e-1)*r+6.89767334985100004550e-1)*r+1.67638483018380384940e+0)*r+2.05319162663775882187e+0)*r+1.0)
    else:
        r=r-5.
        num=(((((((2.01033439929228813265e-7*r+2.71155556874348757815e-5)*r+1.24266094738807843860e-3)*r+2.65321895265761230930e-2)*r+2.96560571828504891230e-1)*r+1.78482653991729133580e+0)*r+5.46378491116411436990e+0)*r+6.65790464350110377720e+0)
        den=(((((((2.04426310338993978564e-15*r+1.42151175831644588870e-7)*r+1.84631831751005468180e-5)*r+7.86869131145613259100e-4)*r+1.48753612908506148525e-2)*r+1.36929880922735805310e-1)*r+5.99832206555887937690e-1)*r+1.0)
    x=num/den
    return -x if q<0. else x
# Exact order-zero, nearest-boundary gaussian filter of fixed one-dimensional float64 arrays.
# A kernel wider than the array folds its tails onto the two end values.
_GAUSS_X2 = {}
# Float64 ndarray inputs go through the same C kernel as numpy.correlate.
_NATIVE_CORR = None
def gaussian_filter1d(values, sigma, cache):
    sigma=float(sigma)
    radius=int(4.0*sigma+.5)
    x2=_GAUSS_X2.get(radius)
    if x2 is None:
        x=np.arange(-radius,radius+1)
        x2=(x*x).astype(np.float64)
        _GAUSS_X2[radius]=x2
    # Scratch by radius; preserve the normalized kernel and summation order.
    weights=cache.get(radius)
    if weights is None:weights=cache[radius]=np.empty(2*radius+1)
    np.multiply(x2,-.5/(sigma*sigma),out=weights)
    np.exp(weights,out=weights)
    weights/=weights.sum()
    n=len(values)
    if radius>=n-1:
        out=_NATIVE_CORR(weights[radius-n+1:radius+n],values,'valid')[::-1]
        cw=weights
        cw.cumsum(out=cw)
        if radius>=n:low=cw[radius-n:radius][::-1]
        else:
            low=np.empty(n);low[:-1]=cw[:radius][::-1];low[-1]=0.
        high=cw[-1]-cw[radius:radius+n][::-1]
        return out+low*values[0]+high*values[-1]
    pad=cache.get(-1)
    if pad is None or len(pad)<n+2*radius:pad=cache[-1]=np.empty(max(901,n+2*radius))
    pad=pad[:n+2*radius]
    pad[:radius]=values[0];pad[radius:radius+n]=values;pad[radius+n:]=values[-1]
    return _NATIVE_CORR(pad,weights,'valid')



# ===================================== Hooks ====================================
# Measured areas with no code yet. Each is called with the player, the step's state and the
# actions proposed so far, after the arms and before the guard, and returns the actions to send.
# The guard fits whatever a hook adds to collateral. Empty, each returns its input and costs a call.
def queue_place(player,state,actions):
    """Arm queue_place (empty). The area: queue place at the touch. A resting order keeps its
    place in the FIFO queue only while it is not cancelled; the players cancel and replace every
    order whose size or price moved. Reads: the state's own orders and the proposed actions; the
    native player's OrderMemory (front, prevq). May change: drop a CancelOrder and the
    PlaceOrder that replaces it, so an order keeps its place. Costs: nothing now."""
    return actions

def midband_retail(player,state,actions):
    """Arm midband_retail (empty). The area: retail the competitor took one tick inside its own
    quote while the price was in 0.10-0.90, where such a quote would have paid against the true
    price and we did not quote. Reads: the belief (NativePlayer.core.bel, .grid, ._belmean;
    S1Player.core.sensor.edges), the state and the proposed actions. May change: add PlaceOrders
    at bid+1 or ask-1. Costs: nothing now."""
    return actions

def low_price_capital(player,state,actions):
    """Arm low_price_capital (empty). The area: the edge below a price of 0.05 that a trader
    with unlimited cash takes and $1,000 cannot hold, for instance YES bought early so that later
    asks are covered and tie up no cash. Reads: the belief, cash and YES in the state, the
    proposed actions. May change: add buys at low ticks and asks that the YES covers. Costs:
    nothing now."""
    return actions

HOOKS=(('queue_place',queue_place),('midband_retail',midband_retail),('low_price_capital',low_price_capital))


# ============================ The spread-1 player: core ============================
_S1_ROOT2=math.sqrt(2.)
_S1_ROOT2PI=math.sqrt(2.*math.pi)
_S1_SCORE_VAR=.02*.02+.0019*.35*.35
_S1_JUMP_NODES=tuple((node*.35,weight) for node,weight in ((-2.2,.06),(-1.3,.17),(-.6,.27),(.6,.27),(1.3,.17),(2.2,.06)))
_S1_BUYPROBE=_S1_SELLPROBE=_S1_SUBTICKS=_S1_DYADIC_SQUARES=()
_S1_QUANTILE01=0.

def _load_s1_constants():
    global _S1_BUYPROBE,_S1_SELLPROBE,_S1_SUBTICKS,_S1_DYADIC_SQUARES,_S1_QUANTILE01
    _S1_QUANTILE01=float(ndtri(.01))
    _S1_BUYPROBE=(None,)+tuple(PlaceOrder(_BUY,i,.01) for i in range(1,100))
    _S1_SELLPROBE=(None,)+tuple(PlaceOrder(_SELL,i,.01) for i in range(1,100))
    _S1_SUBTICKS=tuple((i+.5)*.125 for i in range(800))
    _S1_DYADIC_SQUARES=tuple((i*.125)*(i*.125) for i in range(801))

class S1Sensor:
    def __init__(self):
        if not _S1_SUBTICKS:_load_s1_constants()
        self._centres=None
        self._weights=None
        self._diffusion=.5
        self._last_bid=None
        self._last_ask=None
        self._buy_probes=set()
        self._sell_probes=set()
        self._below_one=0
        self._above_99=0
        self._fair=.5
        self._sigma=.005
        self._jump_prices=[]
        self._remaining=2000
        self._split_value=None

    @staticmethod
    def _normal_cdf(value):
        return .5*(1.+math.erf(value/_S1_ROOT2))

    @staticmethod
    def _normal_density(value):
        return math.exp(-value*value*.5)/_S1_ROOT2PI

    @staticmethod
    def _normal_quantile(probability):
        return float(ndtri(min(1.-1e-9,max(1e-9,probability))))

    def remember_probes(self,buy_ticks,sell_ticks):
        # Call before warning filtering, just as the reference inner strategy.
        self._buy_probes=buy_ticks
        self._sell_probes=sell_ticks

    def observe(self,state):
        bid=state.competitor_best_bid_ticks or 0
        ask=state.competitor_best_ask_ticks or 100
        horizon=max(1,state.steps_remaining)
        lower=bid
        upper=ask
        live_buy=set()
        live_sell=set()
        for order in state.own_orders:
            if order.side==_BUY:
                lower=max(lower,order.price_ticks)
                if order.remaining_quantity<=.01+1e-9:live_buy.add(order.price_ticks)
            else:
                upper=min(upper,order.price_ticks)
                if order.remaining_quantity<=.01+1e-9:live_sell.add(order.price_ticks)
        removed_buy=self._buy_probes-live_buy
        removed_sell=self._sell_probes-live_sell
        if live_sell and removed_sell:
            lower=max(lower,min(max(removed_sell),upper))
        if removed_buy and (live_buy or min(removed_buy)<=1.):
            upper=min(upper,max(min(removed_buy),lower))
        ask_retreat=False
        bid_retreat=False
        if self._last_bid is not None:
            ask_retreat=ask==self._last_ask+1
            bid_retreat=bid==self._last_bid-1
            if ask>self._last_ask+1:lower=max(lower,ask-1)
            if bid<self._last_bid-1:upper=min(upper,bid+1)
        self._last_bid=bid
        self._last_ask=ask
        if lower>upper:lower,upper=upper,lower
        gap=upper-lower
        if 1<=gap<=6:
            count=8*gap
            cell=.125
            centres=_S1_SUBTICKS[8*lower:8*upper]
        else:
            width=max(gap,1e-6)
            count=max(2,min(48,int(round(width*8.))))
            cell=width/count
            centres=[lower+(index+.5)*cell for index in range(count)]
        if self._centres is None:
            weights=[1./count]*count
        else:
            deviation=max(self._diffusion,.6*cell)
            coefficient=-.5/(deviation*deviation)
            floor=.01*deviation
            weights=[]
            oc=self._centres;ow=self._weights;nold=len(oc);lo=0;_exp=math.exp
            if cell==.125 and self._cell==.125:
                # Both centre grids lie on one exact dyadic lattice. Reuse each
                # Gaussian weight by separation, with the same addition order.
                shift=int((centres[0]-oc[0])*8)
                radius=int(40*deviation)
                d=radius*.125
                if d*d*coefficient<=-12.5:radius-=1
                else:
                    d=(radius+1)*.125
                    if d*d*coefficient>-12.5:radius+=1
                first=max(-radius,-shift-count+1)
                last=min(radius,nold-1-shift)
                if first>last:
                    weights=[floor]*count
                else:
                    sq=_S1_DYADIC_SQUARES
                    if first>=0:
                        kernel=[_exp(d2*coefficient) for d2 in sq[first:last+1]]
                    elif last<0:
                        kernel=[_exp(d2*coefficient) for d2 in sq[-first:-last-1:-1]]
                    else:
                        one=[_exp(d2*coefficient) for d2 in sq[:max(-first,last)+1]]
                        kernel=one[-first:0:-1]+one[:last+1]
                    # Full inner products are unrolled left-to-right. Ordinary
                    # addition order is identical to the original mass loop.
                    full=first==-shift-count+1 and last==nold-1-shift
                    if full and nold==8:
                        w0,w1,w2,w3,w4,w5,w6,w7=ow
                        for o in range(count-1,-1,-1):
                            mass=(floor+w0*kernel[o]+w1*kernel[o+1]
                                    +w2*kernel[o+2]+w3*kernel[o+3]
                                    +w4*kernel[o+4]+w5*kernel[o+5]
                                    +w6*kernel[o+6]+w7*kernel[o+7])
                            weights.append(mass)
                    elif full and nold==16:
                        w0,w1,w2,w3,w4,w5,w6,w7,w8,w9,w10,w11,w12,w13,w14,w15=ow
                        for o in range(count-1,-1,-1):
                            mass=(floor+w0*kernel[o]+w1*kernel[o+1]
                                    +w2*kernel[o+2]+w3*kernel[o+3]
                                    +w4*kernel[o+4]+w5*kernel[o+5]
                                    +w6*kernel[o+6]+w7*kernel[o+7]
                                    +w8*kernel[o+8]+w9*kernel[o+9]
                                    +w10*kernel[o+10]+w11*kernel[o+11]
                                    +w12*kernel[o+12]+w13*kernel[o+13]
                                    +w14*kernel[o+14]+w15*kernel[o+15])
                            weights.append(mass)
                    else:
                        for i in range(shift,shift+count):
                            a=max(0,i-radius);b=min(nold,i+radius+1)
                            mass=floor
                            offset=i+first
                            for j in range(a,b):
                                mass+=ow[j]*kernel[j-offset]
                            weights.append(mass)
            else:
                for location in centres:
                    mass=floor
                    while lo<nold:
                        d=location-oc[lo]
                        if d>0. and d*d*coefficient<=-12.5:lo+=1
                        else:break
                    j=lo
                    while j<nold:
                        d=location-oc[j]
                        exponent=d*d*coefficient
                        if exponent>-12.5:mass+=ow[j]*_exp(exponent)
                        elif d<0.:break
                        j+=1
                    weights.append(mass)
            total=sum(weights)
            weights=[mass/total for mass in weights]
        if ask_retreat or bid_retreat:
            revised=[]
            for location,mass in zip(centres,weights):
                multiplier=1.
                if ask_retreat and location<ask-1:multiplier*=.016
                if bid_retreat and location>bid+1:multiplier*=.016
                revised.append(mass*multiplier)
            total=sum(revised)
            weights=[mass/total for mass in revised]
        self._cell=cell
        self._centres=centres
        self._weights=weights
        mean_ticks=sum(location*mass for location,mass in zip(centres,weights))
        second_ticks=sum(location*location*mass for location,mass in zip(centres,weights))
        variance_ticks=max(0.,second_ticks-mean_ticks*mean_ticks)
        probability=min(.998,max(.002,mean_ticks*.01))
        terminal_sd=math.sqrt(horizon*_S1_SCORE_VAR)
        self._below_one=self._below_one+1 if upper<=1 else 0
        if self._below_one:
            excursion=_S1_QUANTILE01-.02*math.sqrt(self._below_one)/terminal_sd
            probability=min(probability,max(1e-6,self._normal_cdf(excursion)))
        # A surviving 99-cent bid certifies the upper tail. Briefly carry
        # that evidence through depletion while the top rival slot stays put.
        age=self._above_99
        if lower>=99. or (age>=4 and bid==98 and upper>=100):
            age+=1
        else:
            age=0
        self._above_99=age
        if age>=10:
            excursion=_S1_QUANTILE01-.85*.02*math.sqrt(age)/terminal_sd
            probability=max(probability,min(1.-1e-6,1.-self._normal_cdf(excursion)))
        standardized=self._normal_quantile(probability)
        self._jump_score=standardized*terminal_sd
        ordinary_sd=self._normal_density(standardized)*.02/terminal_sd
        self._diffusion=ordinary_sd*100.
        self._sigma=math.sqrt(ordinary_sd*ordinary_sd+variance_ticks/10000.+1e-8)
        self._jump_prices=None
        self._fair=probability
        self._remaining=horizon
        return probability,self._sigma,variance_ticks

    def edges(self,price):
        """BUY loss E[(price-p)+], SELL loss E[(p-price)+]."""
        if self._remaining==1:
            return (1.-self._fair)*price,self._fair*(1.-price)
        difference=price-self._fair
        z=difference/self._sigma
        narrow_buy=self._sigma*(self._normal_density(z)+z*self._normal_cdf(z))
        narrow_sell=narrow_buy-difference
        jumps=self._jump_prices
        if jumps is None:
            # The sensor's jump atoms are only read on a pricing call.
            next_sd=math.sqrt(max(1.,self._remaining-1.)*_S1_SCORE_VAR)
            score=self._jump_score;cdf=self._normal_cdf
            jumps=self._jump_prices=[(cdf((score+node)/next_sd),w) for node,w in _S1_JUMP_NODES]
        jump_buy=sum(weight*(price-probability) for probability,weight in jumps if price>probability)
        jump_sell=sum(weight*(probability-price) for probability,weight in jumps if price<probability)
        return .9981*narrow_buy+.0019*jump_buy,.9981*narrow_sell+.0019*jump_sell

# Spread-one inverse-normal approximation.
def s1_quantile(probability):
    # Public fixed inverse-normal approximation and two Newton refinements,
    # authored in Horner form so tail sizing can be checked against reference.
    u = max(1e-9,min(1.-1e-9,probability))
    v = math.sqrt(-2.*math.log(min(u,1.-u)))
    numerator = 2.515517 + v*(.802853 + .010328*v)
    denominator = 1. + v*(1.432788 + v*(.189269 + .001308*v))
    x = v - numerator/denominator
    if u < .5: x = -x
    for iteration in (0,1):
        density = math.exp(-x*x*.5)/_S1_ROOT2PI
        if density < 1e-12: break
        distribution = .5*(1.+math.erf(x/_S1_ROOT2))
        x -= (distribution-u)/density
    return x

_NO_S1_STOCK=(0,0.,0.)

class S1Core:
    """Prices and sizes queues from the interval sensor and optional S1 arms."""
    def __init__(self):
        self.sensor = S1Sensor()
        self.flow = S1Flow() if _arm('s1_flow',1) else None
        self.stock_on = _arm('s1_stock',1)
        self.probes_on = _arm('s1_probes',1)
        self.split_on = _arm('s1_value_split',1)
        self.warning = S1JumpWarning() if _arm('s1_jump_warning',1) else None
        self.serial = 0
        self.ahead = set()
        self.uncovered = {}
        self.flow_ahead = set()
        self.probe_only_step=-1
    def unique(self, prefix):
        self.serial += 1
        return prefix + str(self.serial)
    def _probe_only(self,state,tiny,fair,sigma,bid,ask):
        self.probe_only_step=state.step
        actions=[];free=state.free_cash-2.
        for o in tiny:
            oid=o.order_id
            px=o.price_ticks*.01
            remains=o.remaining_quantity
            if o.side==_BUY:free+=px*remains
            else:free+=(1.-px)*min(self.uncovered.get(oid,0.),remains)
            self.ahead.discard(oid)
            self.uncovered.pop(oid,None)
            actions.append(CancelOrder(oid))
        if self.probes_on:probe_b,probe_a=s1_probes(fair,sigma,bid,ask,free,actions)
        else:probe_b=set();probe_a=set()
        self.sensor.remember_probes(probe_b,probe_a)
        if self.flow is not None:
            self.flow.pending=[]
            self.flow.queue.clear()
        if self.warning is not None:actions=self.warning.warn_actions(state,actions)
        return actions
    def on_step(self, state):
        bid = state.competitor_best_bid_ticks or 0
        ask = state.competitor_best_ask_ticks or 100
        horizon = max(1, state.steps_remaining)
        fair,sigma,observed_variance=self.sensor.observe(state)
        if self.flow is not None:rate,mean_size,flow_value=self.flow.update(state,fair,observed_variance)
        else:rate,mean_size,flow_value=.1265,4.488,.568734
        buys=[];sells=[];tiny=[]
        for o in state.own_orders:
            if o.remaining_quantity>.010000001:
                (buys if o.side==_BUY else sells).append(o)
            else:tiny.append(o)
        # A quiet step at midrange: both margin tests return zero, there is
        # no stock quote, and only the probes can have resting orders.
        if fair>=.1 and not buys and not sells:
            bt=bid+1;at=ask-1;ft=fair*100.
            if ((not 1<=bt<=99 or ft-bt<.3)
                    and (not 1<=at<=99 or at-ft<.3)):
                return self._probe_only(state,tiny,fair,sigma,bid,ask)
        def rounded(q):
            return math.floor(q * 100. + 1e-9) / 100.
        def requested(is_bid, tick):
            if not 1 <= tick <= 99:
                return 0.
            if (fair*100. - tick if is_bid else tick - fair*100.) < .3:
                return 0.
            below, above = self.sensor.edges(tick*.01)
            loss, gain = (below, above) if is_bid else (above, below)
            risk = .15 if fair < .05 else .7
            if is_bid and fair < .05:
                risk *= .08/.15
            if gain <= 0.:
                return 0.
            r = .125 if is_bid else .125 + .5*(rate - .125)
            m = 5. if is_bid else 5. + .5*(mean_size - 5.)
            tail = loss*risk/(r*gain)
            upper_tail = is_bid and tick == 99 and self.sensor._above_99 >= 10
            if tail >= 1.:
                return 10. if upper_tail and fair >= .995 else 0.
            z = s1_quantile(1. - max(1e-6, tail))
            cap = 15000. if not is_bid and tick == 2 and fair < .01 else 3000.
            quantity = math.exp(math.log(m)-.72+1.2*z)/(max(.05,fair) if is_bid else tick*.01)
            if not is_bid and tick==1 and self.sensor._below_one>=max(180,2*horizon):
                # Limit extra short exposure even after an old confirmed floor.
                return min(4500.,max(3200.,quantity))
            if upper_tail:
                return min(45.,max(22.,quantity))
            return min(cap, quantity)
        top_buy=sum(o.remaining_quantity for o in buys if o.price_ticks==bid and o.order_id in self.ahead) if buys else 0
        top_sell=sum(o.remaining_quantity for o in sells if o.price_ticks==ask and o.order_id in self.ahead) if sells else 0
        bt = bid if top_buy >= .02 else bid+1
        at = ask if top_sell >= 2. else ask-1
        qb = requested(True, bt)
        qa = requested(False, at)
        if at == 2 and at < ask and fair < .01:
            qa *= 5.
        if fair>=.1 and not buys and not sells and qb<.01 and qa<.01:
            return self._probe_only(state,tiny,fair,sigma,bid,ask)
        stock = s1_stock(state, fair, flow_value, at, qa, bt) if self.stock_on and fair<.1 else _NO_S1_STOCK
        inventory = state.yes_inventory
        actions = []
        free = state.free_cash - 2.
        retained = set()
        def cancel(order):
            nonlocal free
            actions.append(CancelOrder(order.order_id))
            px = order.price_ticks*.01
            if order.side == _BUY:
                free += px * order.remaining_quantity
            else:
                free += (1.-px)*min(self.uncovered.get(order.order_id,0.),order.remaining_quantity)
            self.ahead.discard(order.order_id)
            self.uncovered.pop(order.order_id,None)
        for order in tiny:cancel(order)
        def retain(orders, tick, desired, is_bid):
            total = 0.
            banked = {}
            level, limit = tick, desired
            for order in orders:
                front = order.order_id in self.ahead
                ot = order.price_ticks
                q = order.remaining_quantity
                deeper = ot < tick if is_bid else ot > tick
                if front and q >= 2. and ot != tick and deeper:
                    small = limit > 0. and (q <= limit if is_bid else q*ot <= min(limit,3000.)*level)
                    if small:
                        retained.add(order.order_id)
                        continue
                    if ot == level:
                        allowed = limit
                    else:
                        allowed = requested(is_bid,ot)
                        level,limit = ot,allowed
                    if allowed > 0. and q <= max(allowed,1.):
                        retained.add(order.order_id)
                        continue
                if is_bid and front and ot < tick and fair*100.-ot >= 4.*100.*sigma:
                    prior = banked.get(ot,0.)
                    if prior+q <= 1.5*requested(True,ot):
                        banked[ot] = prior+q
                        retained.add(order.order_id)
                        continue
                multiplier = 10. if front else 1.5
                persistent = front and (not is_bid and ot <= 2 or is_bid and ot == bid)
                if ot == tick and desired > 0. and (total+q <= max(desired*multiplier,1.) or persistent):
                    total += q
                    retained.add(order.order_id)
                else:
                    cancel(order)
            return total
        held_b = retain(buys,bt,qb,True)
        held_a = retain(sells,at,qa,False)
        add_b = qb-held_b if held_b < .7*qb and bt != bid else 0.
        add_a = qa-held_a if held_a < .7*qa and at != ask else 0.
        buy_stock_tick,buy_stock_quantity,cheap_quantity=stock
        budget = .9*max(free,0.)
        proposed_cost = add_b*bt*.01 + buy_stock_quantity*buy_stock_tick*.01
        if proposed_cost > budget:
            scale = budget/proposed_cost
            add_b *= scale
            buy_stock_quantity *= scale
            proposed_cost = budget
        cheap_quantity = min(cheap_quantity,.6*max(0.,budget-proposed_cost)*100.)
        new = []
        def emit(side,tick,quantity,prefix,uncovered=0.):
            nonlocal free
            q = rounded(quantity)
            if q < .01:
                return
            oid = self.unique(prefix)
            new.append(PlaceOrder(side,tick,q,oid))
            if side == _BUY:
                free -= q*tick*.01 + 1e-6
                if tick > bid:self.ahead.add(oid)
            else:
                self.uncovered[oid] = uncovered
                free -= uncovered*(1.-tick*.01)+1e-6
                if tick < ask:self.ahead.add(oid)
        if cheap_quantity >= .01:
            # Reference cheap stock gets a late flow-only id, not front status
            # in the persistent-main queue ledger.
            q = rounded(cheap_quantity)
            new.append(PlaceOrder(_BUY,1,q))
            free -= q*.01+1e-6
        if buy_stock_tick and buy_stock_quantity >= .01:
            emit(_BUY,buy_stock_tick,buy_stock_quantity,'m')
        if bt >= 1 and add_b >= .01:
            emit(_BUY,bt,add_b,'b')
        split_index=-1
        if at <= 99 and add_a >= .01:
            covered_kept = sum(o.remaining_quantity-min(self.uncovered.get(o.order_id,0.),o.remaining_quantity) for o in sells if o.order_id in retained)
            cover = max(0.,inventory-covered_kept-.1)
            uncovered = max(0.,add_a-cover)
            if uncovered > 0.:
                allowance = max(0.,free-.3*max(state.cash,0.)) if at*.01 >= .1 else 0.
                if horizon < 300 and fair < .05:
                    allowance = max(0.,free*(1.-horizon/300.))
                uncovered = min(uncovered,allowance/(1.-at*.01))
            q = rounded(min(add_a,cover+uncovered))
            if q >= .01:
                if 3<=at<99:split_index=len(new)
                emit(_SELL,at,q,'a',max(0.,q-cover))
        if self.probes_on:probe_b,probe_a=s1_probes(fair,sigma,bid,ask,free,new)
        else:probe_b=set();probe_a=set()
        self.sensor.remember_probes(probe_b,probe_a)
        if split_index>=0:
            order=new[split_index]
            oid=order.client_order_id
            near,far=s1_value_split(self.sensor,state,order.price_ticks,order.quantity,retained,self.ahead,rate,mean_size) if self.split_on else s1_halves(order.quantity)
            reserve=self.uncovered.pop(oid,0.)
            near_unc=max(0.,reserve-far)
            shaped=[]
            if near>=.01:
                shaped.append(PlaceOrder(_SELL,order.price_ticks,near,oid))
                self.uncovered[oid]=near_unc
            if far>=.01:
                far_id=self.unique('a')
                shaped.append(PlaceOrder(_SELL,order.price_ticks+1,far,far_id))
                self.uncovered[far_id]=max(0.,reserve-near_unc)
            new[split_index:split_index+1]=shaped
        # Only the cheap bank bid can be both anonymous and non-probe size.
        if new and new[0].client_order_id is None and new[0].quantity>.01:
            o=new[0]
            new[0]=PlaceOrder(o.side,o.price_ticks,o.quantity,self.unique('f'))
        if self.flow is not None:self.flow.remember(state,retained,new,self.ahead,fair)
        actions.extend(new)
        # Warning acts after bookkeeping, mirroring reference callback timing.
        if self.warning is not None:actions = self.warning.warn_actions(state, actions)
        return actions


# ============================ The spread-1 player: arms ============================
class S1Flow:
    """Fifteen retail rate/size hypotheses updated from front-order fills."""
    cuts = tuple(2.0**i / 32. for i in range(14))

    @staticmethod
    def normal(x):
        return .5*(1.+math.erf(x/_S1_ROOT2))

    def __init__(self):
        rates = [(.1265 + .0495*u, w) for u,w in
                 [(-math.sqrt(.6),5./18.),(0.,4./9.),(math.sqrt(.6),5./18.)]]
        sizes = [(4.488+1.848*u,w) for u,w in
                 [(-.906179845938664,.118463442528095),
                  (-.538469310105683,.239314335249683),
                  (0.,.284444444444444),
                  (.538469310105683,.239314335249683),
                  (.906179845938664,.118463442528095)]]
        self.hypotheses = []
        self.scores = []
        for rate,rw in rates:
            for mean,mw in sizes:
                terms=[]
                for n in range(1,4):
                    v=math.log(1.+(math.exp(1.44)-1.)/n)
                    terms.append((math.log(n*mean)-v/2.,math.sqrt(v),rate**n/math.factorial(n)))
                truncated=[]; right=[]
                for cap in self.cuts:
                    lc=math.log(cap)
                    finite=sum(weight*(self.normal((lc-loc)/scale)-self.normal((math.log(.03)-loc)/scale)) for loc,scale,weight in terms)
                    truncated.append(math.log1p(finite))
                    cumulative=1.+sum(weight*self.normal((lc-loc)/scale) for loc,scale,weight in terms)
                    right.append(math.log(max(1e-100,1.-math.exp(-rate)*cumulative)))
                density_terms=[(loc,scale,w,_S1_ROOT2PI*scale) for loc,scale,w in terms]
                self.hypotheses.append((rate,mean,density_terms,truncated,right))
                self.scores.append(math.log(rw*mw))
        self.truncated_counts=[0]*len(self.cuts)
        self.censored_counts=[0]*len(self.cuts)
        self.safe_count=0
        self.pending=[]
        self.queue=set()
        self.changed=False
        self.rate=.1265
        self.mean=4.488
        self.notional=.568734

    def update(self,state,phat,observed_variance_ticks):
        if not self.pending and (state.step%100!=0 or not (self.changed or state.step==0)):
            return self.rate,self.mean,self.notional
        live={o.order_id:o.remaining_quantity for o in state.own_orders} if self.pending else {}
        threshold=max(.005,2.*math.sqrt(observed_variance_ticks)/100.)
        for conversion,cell,retail_buy,tracked,tick in self.pending:
            filled=max(0.,sum(q-live.get(oid,0.) for oid,q in tracked))
            amount=filled*conversion
            distance=tick/100.-phat if retail_buy else phat-tick/100.
            safe=distance>threshold
            if 0.<amount<.03:
                continue
            if amount>=self.cuts[cell]:
                if safe:
                    self.censored_counts[cell]+=1
                    self.changed=True
                continue
            if safe:
                self.safe_count+=1
            else:
                self.truncated_counts[cell]+=1
            self.changed=True
            if amount>0.:
                y=math.log(amount)
                for i,hyp in enumerate(self.hypotheses):
                    density=sum(w*math.exp(-.5*((y-loc)/scale)**2)/den for loc,scale,w,den in hyp[2])
                    self.scores[i]+=math.log(max(1e-100,density))
        if state.step%100==0 and (self.changed or state.step==0):
            logweights=[]
            for score,h in zip(self.scores,self.hypotheses):
                r,m,terms,trunc,right=h
                v=score-sum(n*trunc[i] for i,n in enumerate(self.truncated_counts) if n)-r*self.safe_count+sum(n*right[i] for i,n in enumerate(self.censored_counts) if n)
                logweights.append(v)
            peak=max(logweights)
            weights=[math.exp(v-peak) for v in logweights]
            mass=sum(weights)
            self.rate=sum(w*h[0] for w,h in zip(weights,self.hypotheses))/mass
            self.mean=sum(w*h[1] for w,h in zip(weights,self.hypotheses))/mass
            self.notional=sum(w*h[0]*h[1] for w,h in zip(weights,self.hypotheses))/mass
            self.changed=False
        return self.rate,self.mean,self.notional

    def remember(self,state,kept_ids,new_place_orders,ahead_ids,phat):
        # The flow queue is independent of the controller's persistence queue:
        # new explicit-id stock orders may also precede the rival.
        if not kept_ids and not any(o.quantity>.01 for o in new_place_orders):
            self.pending=[]
            self.queue.clear()
            return
        self.queue.intersection_update(kept_ids)
        best_b=0;best_a=100
        buy_orders=[];sell_orders=[]
        for o in state.own_orders:
            oid=o.order_id
            q=o.remaining_quantity
            if q<=.01 or oid not in kept_ids:continue
            t=o.price_ticks
            if o.side==_BUY:
                if t>best_b:best_b=t;buy_orders=[(oid,q)]
                elif t==best_b:buy_orders.append((oid,q))
            else:
                if t<best_a:best_a=t;sell_orders=[(oid,q)]
                elif t==best_a:sell_orders.append((oid,q))
        cb=state.competitor_best_bid_ticks or 0
        ca=state.competitor_best_ask_ticks or 100
        for o in new_place_orders:
            q=o.quantity
            if q<=.01:continue
            t=o.price_ticks;oid=o.client_order_id
            if o.side==_BUY:
                if t>cb:self.queue.add(oid)
                if t>best_b:best_b=t;buy_orders=[(oid,q)]
                elif t==best_b:buy_orders.append((oid,q))
            else:
                if t<ca:self.queue.add(oid)
                if t<best_a:best_a=t;sell_orders=[(oid,q)]
                elif t==best_a:sell_orders.append((oid,q))
        self.pending=[]
        for retail_buy,tick,front in ((True,best_a,sell_orders),(False,best_b,buy_orders)):
            if not front or (retail_buy and tick>ca) or (not retail_buy and tick<cb):continue
            if any(oid not in self.queue for oid,q in front):continue
            conversion=tick/100. if retail_buy else max(phat,.05)
            amount=sum(q for oid,q in front)*conversion
            if amount<.04:continue
            cell=max(0,bisect_left(self.cuts,amount-.01)-1)
            self.pending.append((conversion,cell,retail_buy,front,tick))

def s1_stock(state,phat,flow_rm,ask_tick,qa,bid_tick):
    """Size acquisition orders for covering later low-price asks."""
    h=max(1,state.steps_remaining)
    inventory=state.yes_inventory
    p_ticks=phat*100.
    scale=max(.4,min(1.9,1.+.5*(flow_rm/.568734-1.)))
    amt=0; qam=0.
    if ask_tick>=2 and phat<.05:
        wealth=state.cash+inventory*phat
        consumption_price=max(ask_tick,p_ticks+1.)/100.
        wealth_limit=.6*wealth/max(phat,.005)
        short_target=min(scale*.4*min(h,200.)/consumption_price,wealth_limit)
        full_target=min(scale*.6*min(h,2000.)/consumption_price,wealth_limit)
        next_tick=max(1,min(99,int(math.ceil(p_ticks+1e-9))))
        gap=next_tick-p_ticks
        if inventory<full_target and gap<=.15 and bid_tick<=next_tick<ask_tick:
            amt=next_tick
            qam=min(400.,full_target-inventory)
        elif inventory<short_target and bid_tick<=next_tick<ask_tick and next_tick/100.-phat<.02:
            passive=next_tick-1
            if passive>=max(1,bid_tick):
                amt=passive
                qam=min(60.,short_target-inventory)
        elif inventory<full_target and next_tick>=2 and 1.-gap<=.15:
            amt=next_tick-1
            qam=min(400.,full_target-inventory)
    q1=0.
    if 1.<p_ticks<6. and amt!=1:
        q1=max(0.,scale*.6*min(h,2000.)/.01-inventory)
    # Bridge demand supersedes the crossing proposal, but intentionally
    # occurs after the one-tick-bank proposal has been computed.
    if .02<phat<.1:
        bridge_tick=max(2,min(99,int(math.floor(p_ticks))))
        wealth=state.cash+inventory*phat
        target=min(scale*qa,.3*max(wealth,0.)/phat)
        if inventory<target and bridge_tick<ask_tick:
            amt=bridge_tick
            qam=min(3000.,target-inventory)
    return amt,qam,q1

def s1_probes(fair,sigma,bid,ask,free,new):
    """Emit and remember tiny strict-inside observation orders."""
    # Tiny probe purchases/sales exist only strictly between competitor
    # touches and are intentionally independent of the current main sizes.
    probe_b=set();probe_a=set()
    if free<=1. or ask-bid<=1:return probe_b,probe_a
    base_tick=int(math.floor(fair*100.+1e-9))
    probe_radius=max(2,int(math.ceil(200.*sigma)))
    if ask-bid==2:
        t=bid+1
        if base_tick<=t<=base_tick+probe_radius:
            new.append(_S1_BUYPROBE[t]);probe_b.add(t)
        if base_tick+1-probe_radius<=t<=base_tick+1:
            new.append(_S1_SELLPROBE[t]);probe_a.add(t)
    else:
        lo=max(bid+1,1);hi=min(ask-1,99)
        for t in range(max(base_tick,lo),min(base_tick+probe_radius,hi)+1):
            new.append(_S1_BUYPROBE[t]);probe_b.add(t)
        for t in range(max(base_tick+1-probe_radius,lo),min(base_tick+1,hi)+1):
            new.append(_S1_SELLPROBE[t]);probe_a.add(t)
    return probe_b,probe_a

def s1_halves(quantity):
    """s1_value_split off: each new main ask in equal near/far slices, as the split's baseline."""
    units=int(math.floor(quantity*100.+1e-9))
    original_far=units//2
    original_near=units-original_far
    baseline=(original_near/100.,original_far/100.)
    return baseline

# Exact near/far split by a one-step retail-vs-arbitrage value grid.
_S1_SPLIT_SHAPES=[]
for _count in range(1,7):
    _v=math.log1p(math.expm1(1.44)/_count)
    _S1_SPLIT_SHAPES.append((_count,math.exp(_v),_v*.5,math.sqrt(_v)))

class S1AskSplitValue:
    def __init__(self,rate,mean):
        self.parts=[]
        self._callmemo={};self._halfmemo={}
        weight=math.exp(-rate)
        for count,ev,hv,sv in _S1_SPLIT_SHAPES[:4]:
            weight*=rate/count
            m=count*mean
            self.parts.append((weight,m,m*m*ev,math.log(m)-hv,sv))
    @staticmethod
    def cdf(x,_e=math.erfc,_r=math.sqrt(2.)):
        return .5*_e(-x/_r)
    def call(self,threshold,_e=math.erfc,_r=math.sqrt(2.),_log=math.log):
        memo=self._callmemo
        hit=memo.get(threshold)
        if hit is not None:return hit
        result=0.
        if threshold<=0:
            for weight,mean,second,centre,sd in self.parts:
                result+=weight*(mean-threshold)
        else:
            lt=_log(threshold)
            for weight,mean,second,centre,sd in self.parts:
                z=(centre-lt)/sd
                t=mean*(.5*_e(-(z+sd)/_r))-threshold*(.5*_e(-z/_r))
                result+=weight*(t if t>0. else 0.)
        if len(memo)>=4096:memo.clear()
        memo[threshold]=result
        return result
    def half_second(self,threshold,_e=math.erfc,_r=math.sqrt(2.),_log=math.log):
        memo=self._halfmemo
        hit=memo.get(threshold)
        if hit is not None:return hit
        result=0.
        if threshold<=0:
            for weight,mean,second,centre,sd in self.parts:
                term=second-2.*threshold*mean+threshold*threshold
                result+=weight*(term if term>0. else 0.)*.5
        else:
            lt=_log(threshold);tt=threshold*threshold;t2=2.*threshold
            for weight,mean,second,centre,sd in self.parts:
                z=(centre-lt)/sd
                term=second*(.5*_e(-(z+2.*sd)/_r))-t2*mean*(.5*_e(-(z+sd)/_r))+tt*(.5*_e(-z/_r))
                result+=weight*(term if term>0. else 0.)*.5
        if len(memo)>=4096:memo.clear()
        memo[threshold]=result
        return result
    def fill(self,quantity,price,blocker=0.):
        if quantity<=0:return 0.
        value=(self.call(blocker)-self.call(blocker+quantity*price))/price
        return min(quantity,max(0.,value))
    def queued_fill(self,quantity,price,blocker=0.):
        if quantity<=0:return 0.
        end=blocker+quantity*price
        value=(self.half_second(blocker+24.)-self.half_second(blocker+72.)-self.half_second(end+24.)+self.half_second(end+72.))/(48.*price)
        return min(quantity,max(0.,value))
    def score(self,near,far,price,gain_near,loss_near,gain_far_low,gain_far_between,loss_far,old_near=0.,old_far_ahead=0.,old_far_behind=0.):
        near+=old_near;far+=old_far_behind
        second_price=(int(math.floor(price*100.+.5))+1)/100.
        used=near*price
        near_fill=self.fill(near,price)
        # If p<=x: near survives and consumes cash before the far level.
        far_if_low=self.fill(old_far_ahead,second_price,used)+self.queued_fill(far,second_price,used+old_far_ahead*second_price)
        # If x<p<y: near is arbswept before retail, consuming NO retail cash.
        far_if_between=self.fill(old_far_ahead,second_price)+self.queued_fill(far,second_price,old_far_ahead*second_price)
        return gain_near*near_fill+gain_far_low*far_if_low+gain_far_between*far_if_between-loss_near*near-loss_far*(far+old_far_ahead)


def s1_value_split(sensor,state,tick,quantity,retained,ahead,rate,mean):
    units=int(math.floor(quantity*100.+1e-9))
    original_far=units//2
    original_near=units-original_far
    baseline=(original_near/100.,original_far/100.)
    if tick+1!=(state.competitor_best_ask_ticks or 100):return baseline
    older=[o for o in state.own_orders if o.side==_SELL and o.order_id in retained]
    if any(o.price_ticks>tick+1 for o in older):return baseline
    old_near=sum(o.remaining_quantity for o in older if o.price_ticks==tick)
    old_far_ahead=sum(o.remaining_quantity for o in older if o.price_ticks==tick+1 and o.order_id in ahead)
    old_far_behind=sum(o.remaining_quantity for o in older if o.price_ticks==tick+1 and o.order_id not in ahead)
    price=tick/100.
    near_gain,near_loss=sensor.edges(price)
    far_gain,far_loss=sensor.edges((tick+1)/100.)
    if sensor._remaining==1:
        probability_low=1.-sensor._fair
    else:
        probability_low=.9981*S1AskSplitValue.cdf((price-sensor._fair)/sensor._sigma)+.0019*sum(w for p,w in sensor._jump_prices if p<=price)
    far_gain_low=max(0.,near_gain+((tick+1)/100.-price)*probability_low)
    far_gain_between=max(0.,far_gain-far_gain_low)
    # Same half-shrink used by parent fresh-ask marginal sizing.
    key=(rate,mean)
    cached=sensor._split_value
    if cached is None or cached[0]!=key:
        flow=S1AskSplitValue(.125+.5*(rate-.125),5.+.5*(mean-5.))
        sensor._split_value=key,flow
    else:
        flow=cached[1]
    def value(near,far):
        return flow.score(near,far,price,near_gain,near_loss,far_gain_low,far_gain_between,far_loss,old_near,old_far_ahead,old_far_behind)
    base_score=value(*baseline)
    coarse_score=base_score;coarse_j=8
    pairs=[None]*17;scores=[None]*17
    for j in range(0,17,2):
        near_units=units*j//16
        pair=(near_units/100.,(units-near_units)/100.)
        v=value(*pair);pairs[j]=pair;scores[j]=v
        if v>coarse_score+1e-12*max(1.,abs(coarse_score)):
            coarse_score=v;coarse_j=j
    for j in (coarse_j-1,coarse_j+1):
        if not 0<=j<=16:continue
        near_units=units*j//16
        pair=(near_units/100.,(units-near_units)/100.)
        pairs[j]=pair;scores[j]=value(*pair)
    best=baseline;score=base_score
    for j,v in enumerate(scores):
        if v is None:continue
        if v>score+1e-12*max(1.,abs(score)):
            best=pairs[j];score=v
    return best


class S1JumpWarning:
    """Suppress the vulnerable side of new placements after detected jumps."""
    def __init__(self):
        self._warning_mid=None
        self._warning_probability=.05
        self._warning_remaining=0
        self._warning_direction=0
        self._warning_lower=0.
        self._warning_upper=0.
    def warn_actions(self,state,actions):
        # Deliberately AFTER core bookkeeping. A discarded placement may still
        # have a remembered id/probe in the reference; do not 'repair' that here.
        bid=state.competitor_best_bid_ticks
        ask=state.competitor_best_ask_ticks
        if bid is None or ask is None:return actions
        midpoint=(ask+bid)*.5
        previous=self._warning_mid
        self._warning_mid=midpoint
        if previous is None:return actions
        movement=midpoint-previous
        evidence=abs(movement)/2.
        if (state.sell_filled_quantity>0 and movement>0) or (state.buy_filled_quantity>0 and movement<0):evidence+=.5
        ratio=2.718281828**(3.*(evidence-1.))
        prior=self._warning_probability
        posterior=prior*ratio/(prior*ratio+1.-prior)
        self._warning_probability=min(.95,max(.01,.9*posterior+.01))
        if posterior>.5 and abs(movement)>=1:
            self._warning_remaining=4
            self._warning_direction=1 if movement>0 else -1
            self._warning_lower=min(previous,midpoint)
            self._warning_upper=max(previous,midpoint)
        elif self._warning_remaining>0:
            if abs(movement)>=1 and (movement>0)==(self._warning_direction>0):
                self._warning_lower=min(self._warning_lower,midpoint)
                self._warning_upper=max(self._warning_upper,midpoint)
                self._warning_remaining=4
            else:self._warning_remaining-=1
        if self._warning_remaining<=0:return actions
        result=[]
        for action in actions:
            if isinstance(action,PlaceOrder):
                if self._warning_direction>0 and action.side==_SELL and action.price_ticks<=self._warning_upper+1:continue
                if self._warning_direction<0 and action.side==_BUY and action.price_ticks>=self._warning_lower-1:continue
            result.append(action)
        return result


# ============================ The native player: core ============================
_SQRT2=1.4142135623730951

def _moments(q,ahead,price,mean,sigma,need2=True):
    if q<=0:return 0.,0.
    scale=mean/price
    if ahead==0.:
        u=q+ahead
        z=(math.log(u/scale)+sigma*sigma*.5)/sigma
        tail=.5*math.erfc(z*.7071067811865475)
        e1=scale*.5*math.erfc(-(z-sigma)*.7071067811865475)+u*tail
        e2=(scale*scale*math.exp(sigma*sigma)*.5*math.erfc(-(z-2*sigma)*.7071067811865475)+u*u*tail) if need2 else 0.
        return e1,e2
    def raw(u):
        if u<=0:return 0.,0.
        z=(math.log(u/scale)+sigma*sigma*.5)/sigma
        tail=.5*math.erfc(z*.7071067811865475)
        e1=scale*.5*math.erfc(-(z-sigma)*.7071067811865475)+u*tail
        e2=(scale*scale*math.exp(sigma*sigma)*.5*math.erfc(-(z-2*sigma)*.7071067811865475)+u*u*tail) if need2 else 0.
        return e1,e2
    m1,m2=raw(q+ahead)
    if ahead>0:
        a1,a2=raw(ahead);e1=max(0.,m1-a1);e2=max(0.,m2-a2-2*ahead*e1)
        return e1,e2
    return m1,m2

def _fast_value(q,ahead,loss,gain,arb,r,price,mean,sigma,gap,curv,shadow,buy):
    need2=(buy and gap>0) or (not buy and gap+q>0)
    e1,e2=_moments(q,ahead,price,mean,sigma,need2)
    val=-q*loss+r*gain*e1
    sv=shadow*(arb*q+r*(1-arb)*e1)
    val+=sv if buy else -sv
    if buy:
        if gap>0:
            cc=min(q,gap)
            if cc==q:c1,c2=e1,e2
            else:c1,c2=_moments(cc,ahead,price,mean,sigma)
            val+=curv*(arb*(2*gap*cc-cc*cc)+r*(1-arb)*(2*gap*c1-c2))
    elif gap>=0:
        val-=curv*(arb*(2*gap*q+q*q)+r*(1-arb)*(2*gap*e1+e2))
    elif q>-gap:
        D=-gap;d1,d2=_moments(D,ahead,price,mean,sigma)
        val-=curv*(arb*(q-D)**2+r*(1-arb)*max(0.,e2-d2-2*D*(e1-d1)))
    return val

def _best_grid(cap,ahead,loss,gain,arb,r,price,mean,sigma,gap,curv,shadow,buy,qgrid,loc,ss,_erfc=math.erfc,_log=math.log):
    w0=shadow+2*curv*max(gap,0.)
    if buy:upper=-loss+r*gain+w0*(arb+r*(1-arb))
    else:upper=-loss-w0*arb+max(0.,r*(gain-w0*(1-arb)))
    if upper < -1e-15:return -1e100,0.
    coef=r*(1-arb)
    lo=0;hi=len(qgrid)
    while lo<hi:
        j=(lo+hi)//2
        q=min(cap,qgrid[j])
        surv=.5*_erfc((_log(max(1e-100,(q+ahead)*price))-loc)/ss)
        w=shadow if buy else -shadow
        if buy:
            if q<gap:w+=2*curv*(gap-q)
        elif q>-gap:w-=2*curv*(gap+q)
        derivative=-loss+r*gain*surv+w*(arb+coef*surv)
        if derivative>0:lo=j+1
        else:hi=j
    cand=[]
    if lo>0:cand.append(min(cap,qgrid[lo-1]))
    if lo<len(qgrid):cand.append(min(cap,qgrid[lo]))
    if arb>1e-12 and curv>0:
        bonus=2*curv*gap if buy else -2*curv*gap
        oq=(bonus*arb-loss)/(2*curv*arb)
        if oq>0:cand.append(min(cap,max(.01,oq)))
    best=(-1e100,0.)
    for q in cand:
        v=_fast_value(q,ahead,loss,gain,arb,r,price,mean,sigma,gap,curv,shadow,buy)
        if v>best[0]:best=(v,q)
    return best

def _price_value23(q,ahead,loss,gain,arb,r,price,mean,sigma,gap,curv,shadow,buy):
    need2=(buy and gap>0) or (not buy and gap+q>0)
    e1,e2=_moments(q,ahead,price,mean,sigma,need2)
    val=-q*loss+r*gain*e1
    if shadow:
        sv=shadow*(arb*q+r*(1-arb)*e1)
        val+=sv if buy else -sv
    if buy:
        if gap>0:
            cc=min(q,gap)
            if cc==q:c1,c2=e1,e2
            else:c1,c2=_moments(cc,ahead,price,mean,sigma)
            val+=curv*(arb*(2*gap*cc-cc*cc)+r*(1-arb)*(2*gap*c1-c2))
    elif gap>=0:
        val-=curv*(arb*(2*gap*q+q*q)+r*(1-arb)*(2*gap*e1+e2))
    elif q>-gap:
        D=-gap;d1,d2=_moments(D,ahead,price,mean,sigma)
        val-=curv*(arb*(q-D)**2+r*(1-arb)*max(0.,e2-d2-2*D*(e1-d1)))
    return val

def _price_grid23(cap,ahead,loss,gain,arb,r,price,mean,sigma,gap,curv,shadow,buy,qgrid,loc,ss,_erfc=math.erfc,_log=math.log):
    if ahead or shadow:
        return _best_grid(cap,ahead,loss,gain,arb,r,price,mean,sigma,gap,curv,shadow,buy,qgrid,loc,ss)
    c2=2*curv
    w0=c2*max(gap,0.)
    rg=r*gain
    aa=1-arb
    if buy:upper=-loss+rg+w0*(arb+r*aa)
    else:upper=-loss-w0*arb+max(0.,r*(gain-w0*aa))
    if upper < -1e-15:return -1e100,0.
    coef=r*aa
    lo=0;hi=len(qgrid)
    if buy:
        while lo<hi:
            j=(lo+hi)//2
            q=qgrid[j]
            if q>cap:q=cap
            surv=.5*_erfc((_log(q*price)-loc)/ss)
            w=c2*(gap-q) if q<gap else 0.
            derivative=-loss+rg*surv+w*(arb+coef*surv)
            if derivative>0:lo=j+1
            else:hi=j
    else:
        while lo<hi:
            j=(lo+hi)//2
            q=qgrid[j]
            if q>cap:q=cap
            surv=.5*_erfc((_log(q*price)-loc)/ss)
            w=-(c2*(gap+q)) if q>-gap else 0.
            derivative=-loss+rg*surv+w*(arb+coef*surv)
            if derivative>0:lo=j+1
            else:hi=j
    bestv=-1e100;bestq=0.
    if lo:
        q=qgrid[lo-1]
        if q>cap:q=cap
        v=_price_value23(q,0.,loss,gain,arb,r,price,mean,sigma,gap,curv,0.,buy)
        if v>bestv:bestv=v;bestq=q
    if lo<len(qgrid):
        q=qgrid[lo]
        if q>cap:q=cap
        if q!=bestq:
            v=_price_value23(q,0.,loss,gain,arb,r,price,mean,sigma,gap,curv,0.,buy)
            if v>bestv:bestv=v;bestq=q
    if arb>1e-12 and curv>0:
        bonus=c2*gap if buy else -c2*gap
        oq=(bonus*arb-loss)/(c2*arb)
        if oq>0.:
            q=min(cap,max(.01,oq))
            if q!=bestq:
                v=_price_value23(q,0.,loss,gain,arb,r,price,mean,sigma,gap,curv,0.,buy)
                if v>bestv:bestv=v;bestq=q
    return bestv,bestq

def _arb_derivative(q,loss,arb,gap,c2):
    return -loss+(c2*(gap-q) if q<gap else 0.)*arb

def _price_arb23(cap,loss,arb,gap,curv,qgrid):
    hi=len(qgrid)
    c2=2*curv
    oq=0.
    if arb>1e-12 and curv>0.:
        oq=(c2*gap*arb-loss)/(c2*arb)
        if _arb_derivative(cap,loss,arb,gap,c2)>0.:
            lo=hi
        else:
            # Use the quadratic's root as a hint; verify and correct the
            # bracket with the original derivative, including exact zeros.
            lo=bisect_left(qgrid,oq)
            while lo and _arb_derivative(min(cap,qgrid[lo-1]),loss,arb,gap,c2)<=0.:
                lo-=1
            while lo<hi and _arb_derivative(min(cap,qgrid[lo]),loss,arb,gap,c2)>0.:
                lo+=1
    else:
        lo=0
        top=hi
        while lo<top:
            j=(lo+top)//2
            if _arb_derivative(min(cap,qgrid[j]),loss,arb,gap,c2)>0.:lo=j+1
            else:top=j
    cand=[]
    if lo>0:cand.append(min(cap,qgrid[lo-1]))
    if lo<hi:cand.append(min(cap,qgrid[lo]))
    if oq>0.:cand.append(min(cap,max(.01,oq)))
    best=-1e100,0.
    for q in cand:
        cc=min(q,gap)
        v=-q*loss+curv*(arb*(2*gap*cc-cc*cc))
        if v>best[0]:best=(v,q)
    return best

def _fast_gauss(x,lo,hi,sigma,cache,bins):
    # Compact support Gaussian with nearest-boundary tails.
    rad=int(4.*sigma+.5)
    if bins:
        log=math.log(sigma)
        j=int(math.floor(log*bins+.5))
        k=(rad,j)
        prev=cache.get(k)
        if prev is not None:w,tail=prev
        else:sigma=math.exp(j/bins)
    else:prev=None
    if prev is None:
        x2=_GAUSS_X2.get(rad)
        if x2 is None:
            r=np.arange(-rad,rad+1)
            x2=(r*r).astype(np.float64)
            _GAUSS_X2[rad]=x2
        if bins:
            w=np.exp(-.5/(sigma*sigma)*x2);w/=w.sum()
            tail=w[:rad].cumsum()
            cache[k]=w,tail
        else:
            w=cache.get(rad)
            if w is None:w=cache[rad]=np.empty(2*rad+1)
            np.multiply(x2,-.5/(sigma*sigma),out=w)
            np.exp(w,out=w)
            w/=w.sum()
            tail=w[:rad] if lo==0 or hi==301 else None
    y=_NATIVE_CORR(x,w,'full')
    if not bins and tail is not None:tail.cumsum(out=tail)
    st=lo-rad;en=hi+rad
    if st<0:y=y[-st:];st=0
    if en>301:y=y[:301-st];en=301
    if lo==0:
        m=min(rad,en)
        y[:m]+=tail[::-1][:m]*x[0]
    if hi==301:
        m=min(rad,301-st)
        y[-m:]+=tail[-m:]*x[-1]
    return y,st,en

def _fast_diffuse(x,lo,hi,body_sigma,jump_sigma,jump,cache,bins):
    y,a,b=_fast_gauss(x[lo:hi],lo,hi,body_sigma,cache,bins)
    z,c,d=_fast_gauss(y,a,b,jump_sigma,cache,bins)
    y*=1-jump
    z*=jump;z[a-c:b-c]+=y
    x.fill(0.)
    x[c:d]=z
    return x

def _best_arb_buy(cap,loss,arb,gap,curv,qgrid):
    lo=0;hi=len(qgrid)
    while lo<hi:
        j=(lo+hi)//2
        q=min(cap,qgrid[j])
        derivative=-loss+(2*curv*(gap-q) if q<gap else 0.)*arb
        if derivative>0.:lo=j+1
        else:hi=j
    cand=[]
    if lo>0:cand.append(min(cap,qgrid[lo-1]))
    if lo<len(qgrid):cand.append(min(cap,qgrid[lo]))
    if arb>1e-12 and curv>0:
        oq=(2*curv*gap*arb-loss)/(2*curv*arb)
        if oq>0:cand.append(min(cap,max(.01,oq)))
    best=-1e100,0.
    for q in cand:
        cc=min(q,gap)
        v=-q*loss+curv*(arb*(2*gap*cc-cc*cc))
        if v>best[0]:best=(v,q)
    return best

_FLOOR_NODES=((-2.420906,.02),(-1.610138,.08),(-.861164,.15),(-.324663,.25),(.324663,.25),(.861164,.15),(1.610138,.08),(2.420906,.02))
def _floor_prob(H,jvar=.416*.416,_e=math.erfc):
    lam=.0019*H
    w0=math.exp(-lam);w1=lam*w0;w2=max(1e-12,1.-w0-w1)
    d=.0004*H
    i0=.7071067811865476/math.sqrt(d)
    i1=.7071067811865476/math.sqrt(d+jvar)
    i2=.7071067811865476/math.sqrt(d+jvar*max(2.,(lam-w1)/w2))
    def p(z):
        return .5*(w0*_e(-z*i0)+w1*_e(-z*i1)+w2*_e(-z*i2))
    return p

def _floor_score(H,_f=_floor_prob):
    p=_f(H)
    a=-5.;b=0.
    for unused in range(12):
        m=(a+b)*.5
        if p(m)>.01:b=m
        else:a=m
    return (b+a)*.5

def _floor_atoms(H,z,latent_sd,jump_sd):
    total_s2=latent_sd**2+.0004
    nojump=math.sqrt(total_s2)
    jump=math.sqrt(total_s2+jump_sd**2)
    if H==1:
        ph=.9981*ndtr(z/nojump)+.0019*ndtr(z/jump)
        return ph,((1.,ph),)
    p=_floor_prob(H-1,jump_sd**2)
    samples=[(p(z+nojump*x),w*.9981) for x,w in _FLOOR_NODES]
    samples.extend((p(z+jump*x),w*.0019) for x,w in _FLOOR_NODES)
    mean=sum(x*w for x,w in samples)
    return mean,samples

class FloorScoreRisk:
    """Ask-side forecast after a long, confirmed stay below the first tick.

    The latent-score threshold for p=1% recedes as the horizon shortens.
    A sustained episode is more informative than a fresh sub-penny reading:
    use the old threshold, propagate latent-score uncertainty during the stay,
    and then integrate the next Gaussian move and possible upward jump.
    """
    def __init__(self):
        self.age=0
        self.entry_h=0
        self.entry_z=0.
    def observe(self,H,b,hi,past_mean,next_mean):
        if b==0 and past_mean<1.25:self.age+=1
        else:self.age=0
        D=self.age
        if D<30 or H>500 or hi>1 or next_mean>=2.:return None
        hw=H+D
        if self.entry_h!=hw:
            self.entry_h=hw
            self.entry_z=_floor_score(hw)
        drift_sd=.02*math.sqrt(D)
        z=self.entry_z-.5*drift_sd
        post_sd=.67*drift_sd
        # Stay out of the approximation if the old threshold and
        # observation uncertainty do not separate us from a fresh crossing.
        if _floor_score(H)-z<1.8*post_sd:return None
        return _floor_atoms(H,z,post_sd,.416)

class NativeCore:
    """The native player's core: a belief over the price on 301 grid points, updated from the
    fills and non-fills of its own orders and the competitor's quotes, diffused and mixed with
    jumps each step; one bid and one ask priced from it by a one-step value with an inventory
    shortage utility and sized on a grid; the orders it keeps and cancels. It reads the frame's
    OrderMemory (self.mem) and the retail model (rate, meanretail, sigma) that flow_learning
    sets. Its arms inside it: probes (self.probe_arm), yes_stock (self.stock_arm) and
    recalibration (self.recal), each None when off; controlled says the sell controller prices
    the asks in 0.10-0.90."""
    fast=False
    kernelbins=0
    inventorycap=3000.
    inside_probe_stop=1.
    s4_stop_calibrate=False
    obsfloor=1e-9
    askmax=10000.
    askmax_spread2=3000.
    askmax_other=3000.
    askgate=True
    replacement=1.
    replacementH=50.
    halfobs=True
    retention=1.
    gatequeue=True
    compdepth=48.
    depthfloor=5.
    depthdecay=1.
    depthlevels=1
    jump=.0018
    volfactor=.9
    N=301
    maxsize=1500.
    targetnotional=100.
    kappa=.004
    dynamic=False
    gridfix=False
    compsoft=.02
    changeobs=True
    rate=.125
    meanretail=4.5
    sigma=1.2
    jumpfactor=160.
    def __init__(self,mem,probe_arm,stock_arm,recal,controlled):
        self.mem=mem;self.probe_arm=probe_arm;self.stock_arm=stock_arm;self.recal=recal;self.controlled=controlled
        self.depthbid=self.compdepth;self.depthask=self.compdepth
        self.marketspread=None
        self.grid=np.linspace(0,100,self.N)
        self.dx=100/(self.N-1)
        self.bel=None
        self.grid_floor=np.floor(self.grid)
        self.grid_ceil=np.ceil(self.grid)
        self.grid_floor_idx=np.clip(self.grid_floor.astype(int),0,100)
        self.grid_ceil_idx=np.clip(self.grid_ceil.astype(int),0,100)
        self.grid_conv=np.maximum(self.grid*.01,.05)
        self.ticks99=np.arange(1,100).astype(np.float64)
        # Same per-tick float64 operations, batched to avoid 200 tiny NumPy calls.
        ticks=np.arange(101,dtype=np.float64)[:,None]
        bound_low=np.clip((self.grid-ticks)/self.dx+.5,0,1)
        bound_high=np.clip((ticks-self.grid)/self.dx+.5,0,1)
        bound_low[0]=1.
        bound_high[100]=1.
        self.bound_low=list(bound_low)
        self.bound_high=list(bound_high)
        self.oldb=0;self.olda=100
        self.serial=0
        self._floor_age=0
        self._s2_cash_ask=None
        self._s2_uncovered=0.
        self.ilow=np.searchsorted(self.grid,np.arange(1,100),side="left")
        self.ihigh=np.searchsorted(self.grid,np.arange(1,100),side="right")
        self.qgrid=np.array([.1,.25,.5,1.,2.,3.,4.,6.,8.,12.,16.,24.,32.,48.,64.,96.,128.,192.,256.,384.,512.,768.,1024.,1500.,2000.,3000.,5000.,7500.,10000.])
        self.qgrid_scalar=tuple(float(q) for q in self.qgrid)
        self._K101=np.arange(101,dtype=np.float64)
        self._K101i=np.arange(101)
        self._bmcache={}
        self._wcache={}
        self._kernels={}
        self._s3obcache={}
        self._sale_moves={};self._buy_moves={}
        global _NATIVE_CORR
        if _NATIVE_CORR is None:_NATIVE_CORR=np._core.multiarray.correlate2
        if isinstance(mem,FastOrderMemory):
            self._obcache={}
            self.inventorycap=3000.
        self._pref=np.empty(self.N+1);self._pref[0]=0.
        self._moment=np.empty(self.N+1);self._moment[0]=0.
        self._moment_prod=np.empty(self.N)
        self._pref_reader=memoryview(self._pref)
        self._moment_reader=memoryview(self._moment)
        self._ceil_first=np.searchsorted(self.grid_ceil_idx,np.arange(102))
    def _tails(self,sub,_log=math.log,_e=math.erfc,_s=_SQRTH):
        rate=self.rate;mr=self.meanretail;sg=self.sigma;h=.5*sg**2
        if len(sub)>=40:
            z=-(np.log(np.maximum(sub,.000001)/mr)+h)/sg
            return np.maximum(1e-12,rate*np.interp(z,_TAIL_Z,_TAIL_CDF))
        out=[]
        for v in sub.tolist():
            t=rate*(.5*_e(((_log((v if v>.000001 else .000001)/mr)+h)/sg)*_s))
            out.append(t if t>1e-12 else 1e-12)
        return np.array(out)
    def boundmask(self,lo,hi):
        if not self.halfobs:return np.where((self.grid>=lo)&(self.grid<=hi),1.,1e-40)
        cache=self._bmcache
        m=cache.get((lo,hi))
        if m is None:
            m=np.maximum(1e-40,np.minimum(self.bound_low[lo],self.bound_high[hi]))
            cache[(lo,hi)]=m
        return m
    def new_id(self,prefix):
        self.serial+=1
        return prefix+str(self.serial)
    def propose(self,s,b,a):
        mem=self.mem
        if self.marketspread is None:
            self.marketspread=(a-b+1)//2
        fast=self.fast
        compact=False
        extra=self.depthlevels if self.marketspread==1 or not self.gatequeue else 0
        lo=0;hi=100
        if self.bel is None:
            mid=(b+a)/2
            self.bel=np.where(abs(self.grid-mid)<.5001,1.,1e-20)
            self.bel/=self.bel.sum()
        else:
            fb=mem.prevbuy.copy() if s.buy_filled_quantity>0 else None
            fa=mem.prevask.copy() if s.sell_filled_quantity>0 else None
            for o in s.own_orders:
                if o.side==_BUY:
                    if o.price_ticks>lo:lo=o.price_ticks
                    if fb is not None:fb[o.price_ticks]-=o.remaining_quantity
                else:
                    if o.price_ticks<hi:hi=o.price_ticks
                    if fa is not None:fa[o.price_ticks]-=o.remaining_quantity
            # Refreshed competitor quotes can be stale after retail fills.
            _wcache=self._wcache
            w0=_wcache.get((b,a))
            if w0 is None:
                w0=np.where(self.grid>=b,1.,self.compsoft)*np.where(self.grid<=a,1.,self.compsoft)
                _wcache[(b,a)]=w0
            compact=fast and hi>=lo and hi-lo<30
            if compact:
                sl=slice(3*lo,3*hi+1)
                w0=w0[sl]
                fl=self.grid_floor_idx[sl];ce=self.grid_ceil_idx[sl]
                conv=self.grid_conv[sl]
            else:
                fl=self.grid_floor_idx;ce=self.grid_ceil_idx;conv=self.grid_conv
            # Notional of a few probes is negligible in the lognormal tail:
            # requiring any retail event costs one factor of the arrival rate.
            w=w0
            if self.marketspread==3:
                stop=self.inside_probe_stop
                if stop!=1. and not 10.<=self._belmean<=90.:stop=1.
                # If our best surviving inside quote was untouched, retail
                # would have had to stop within the tiny filled probe volume.
                if fb is not None and s.buy_filled_quantity<=.06 and b>=self.oldb:
                    hit=fb>1e-7
                    u=int(hit.argmax())
                    if hit[u]:
                        j=max(int(self.ilow[u-1]),int(lo/self.dx)-2)
                        k=min(self.N,int(hi/self.dx)+3)
                        if j<k:
                            cut=stop if stop!=1. and lo>self.oldb and fb[lo]<1e-7 else 1.
                            w=w0.copy();w[j:k]*=self.rate*cut
                    fb=None
                if fa is not None and s.sell_filled_quantity<=.06 and a<=self.olda:
                    hit=fa[::-1]>1e-7
                    u=int(hit.argmax())
                    if hit[u]:
                        j=self._ceil_first[max(0,lo-1)]
                        k=self._ceil_first[min(101,hi+2,101-u)]
                        if j<k:
                            if w is w0:w=w0.copy()
                            cut=stop if stop!=1. and hi<self.olda and fa[hi]<1e-7 else 1.
                            w[j:k]*=self.rate*cut
                    fa=None
            if fb is not None:np.maximum(0,fb,out=fb)
            if fa is not None:np.maximum(0,fa,out=fa)
            reqsell=None;reqk=None
            if fb is not None:
                cb=fb.cumsum()
                reqsell=cb[fl]*conv
            if fa is not None:
                # The buy requirement depends on a grid point only through its ceiling tick k: it is
                # computed once a tick (ceil(grid) takes the values 0..100 as floats, as K does).
                reqk=(fa*self._K101i*.01)[::-1].cumsum()[::-1]
            if self.changeobs:
                # Competitor sweep terms depend on its two touches only.
                # Keep them separate from the current participant fills.
                if a>self.olda:
                    key=(a,self.olda)
                    sweep=self._buy_moves.get(key)
                    if sweep is None:
                        n=np.clip(a-self._K101,0,a-self.olda)
                        sweep=self._buy_moves[key]=np.maximum(0,(n-.5)*40.)
                    if reqk is None:reqk=sweep
                    else:reqk+=sweep
                if b<self.oldb:
                    key=(b,self.oldb)
                    sweep=self._sale_moves.get(key)
                    if sweep is None:
                        n=np.clip(self.grid_floor-b,0,self.oldb-b)
                        sweep=self._sale_moves[key]=np.maximum(0,(n-.5)*40.*np.maximum(self.grid,5.)/max(1.,self.oldb))
                    if compact:sweep=sweep[sl]
                    if reqsell is None:reqsell=sweep
                    else:reqsell+=sweep
            # Only entries above 1e-7 change w; the rest multiply it by exactly 1.
            if reqk is not None:
                if self.marketspread>=2:
                    klo=max(0,lo-1);khi=min(101,hi+2)
                    hitk=(reqk[klo:khi]>1e-7).nonzero()[0]+klo
                else:
                    hitk=(reqk>1e-7).nonzero()[0]
                if len(hitk):
                    fk=np.ones(101);tails=self._tails(reqk[hitk])
                    # An untouched surviving inside ask proves any retail
                    # buying stopped on our orders, rather than sweeping out.
                    if self.marketspread in (2,4) and hi<self.olda and fa is not None and fa[hi]<1e-7 and 10.<=self._belmean<=90.:
                        if self.marketspread==2:tails*=.05
                        elif self.s4_stop_calibrate and s.sell_filled_quantity<=.06:tails*=.025
                        else:tails*=.18
                    fk[hitk]=tails
                    w=w*fk[ce]
            if reqsell is not None:
                if self.marketspread>=2 and not compact:
                    glo=max(0,int(lo/self.dx)-2);ghi=min(self.N,int(hi/self.dx)+3)
                    hit=(reqsell[glo:ghi]>1e-7).nonzero()[0]+glo
                else:
                    hit=(reqsell>1e-7).nonzero()[0]
                if len(hit):
                    if w is w0:w=w0.copy()
                    tails=self._tails(reqsell[hit])
                    # Same evidence when retail selling stops before an inside bid.
                    if self.marketspread in (2,4) and lo>self.oldb and fb is not None and fb[lo]<1e-7 and 10.<=self._belmean<=90.:
                        if self.marketspread==2:tails*=.05
                        elif self.s4_stop_calibrate and s.buy_filled_quantity<=.06:tails*=.025
                        else:tails*=.18
                    w[hit]*=tails
            if w is w0:
                if fast:
                    key=(lo,hi,b,a)
                    w=self._bmcache.get(key)
                    if w is None:
                        bm=self.boundmask(lo,hi)
                        w=w0*(bm[sl] if compact else bm)
                        self._bmcache[key]=w
                elif self.marketspread>=4:
                    key=(b,a,lo,hi)
                    w=self._obcache.get(key)
                    if w is None:w=self._obcache[key]=w0*self.boundmask(lo,hi)
                elif self.marketspread==3:
                    key=(b,a,lo,hi)
                    w=self._s3obcache.get(key)
                    if w is None:w=self._s3obcache[key]=w0*self.boundmask(lo,hi)
                else:w=w0*self.boundmask(lo,hi)
            else:
                bm=self.boundmask(lo,hi)
                w*=bm[sl] if compact else bm
            if compact:
                post=self.bel[sl]
            else:post=self.bel
            post+=self.obsfloor
            post*=w
            mass=float(post.sum())
            if mass<1e-30:
                compact=False
                self.bel=self.boundmask(lo,hi).copy()
                self.bel*=np.where(self.grid>=b,1.,self.compsoft)*np.where(self.grid<=a,1.,self.compsoft)
                self.bel/=self.bel.sum()
            else:post/=mass
        if self.marketspread==1 or not self.gatequeue:
            # Estimate depletion of currently visible competitor touch queues.
            # Unfilled inside own order proves no retail reached competitor.
            live={o.order_id:o.remaining_quantity for o in s.own_orders}
            pp=max(.05,float(self.bel@self.grid)*.01)
            for side,bt,old,attr in [(_BUY,b,self.oldb,'depthbid'),(_SELL,a,self.olda,'depthask')]:
                dep=self.depthbid if attr=='depthbid' else self.depthask
                if bt!=old:dep=self.compdepth
                elif bt not in (0,100):
                    ahead=[(cid,t,q) for cid,sd,t,q in mem.lastmain if sd==side and (((t>bt) if side==_BUY else (t<bt)) or (t==bt and cid in mem.front))]
                    blocked=any(live.get(cid,0)>.005 for cid,t,q in ahead)
                    if not blocked:
                        primary=max(ahead,key=_second) if side==_BUY and ahead else (min(ahead,key=_second) if ahead else None)
                        if primary is None:flow=self.rate*self.meanretail
                        else:
                            cid,t,q=primary
                            arb=float(self.bel@(self.grid<t)) if side==_BUY else float(self.bel@(self.grid>t))
                            threshold=q*(pp if side==_BUY else t*.01)
                            z=(math.log(max(1e-100,threshold)/self.meanretail)+.5*self.sigma**2)/self.sigma
                            tail=float(ndtr(-z))
                            excess=max(0,self.meanretail*float(ndtr(self.sigma-z))-threshold*tail)/max(1e-12,tail)
                            flow=arb*self.rate*self.meanretail+(1-arb)*excess
                        if side==_BUY:flow*=bt*.01/pp
                        dep=max(self.depthfloor,dep-self.depthdecay*flow)
                if attr=='depthbid':self.depthbid=dep
                else:self.depthask=dep
        self.oldb=b;self.olda=a
        self.volfactor=.8 if self.marketspread==2 else .9
        mean=float(post@self.grid[sl]) if compact else float(self.bel@self.grid)
        predictive_mean_target=mean
        p=min(.99999,max(.00001,mean/100))
        vol=100*self.volfactor*math.exp(-.5*float(ndtri(p))**2)/math.sqrt(2*math.pi*max(1,s.steps_remaining))
        body_sigma=max(.25,vol/self.dx)
        jstd=math.sqrt(self.jumpfactor/max(10,s.steps_remaining))*100*(p*(1-p))**.7
        jump_sigma=max(1,jstd/self.dx)
        if compact:
            self.bel=_fast_diffuse(self.bel,3*lo,3*hi+1,body_sigma,jump_sigma,self.jump,self._kernels,self.kernelbins)
        else:
            self.bel=gaussian_filter1d(self.bel,body_sigma,self._kernels)
        if compact:pass
        elif self.marketspread>=3 and self.bel[0]+self.bel[-1]<1e-12:
            jmean=float(self.bel@self.grid)
            delta=self.grid-jmean
            delta*=delta
            jvar=float(self.bel@delta)
            if jvar<.04*jstd*jstd:
                jvariance=jstd*jstd+jvar
                delta*=(-.5/jvariance)
                np.exp(delta,out=delta)
                delta*=self.dx/math.sqrt(2*math.pi*jvariance)
                jb=delta
            else:
                jb=gaussian_filter1d(self.bel,jump_sigma,self._kernels)
        else:
            jb=gaussian_filter1d(self.bel,jump_sigma,self._kernels)
        if not compact:
            self.bel*=1-self.jump
            jb*=self.jump
            self.bel+=jb
        self.bel/=self.bel.sum()
        mean=float(self.bel@self.grid)
        if self.marketspread>=3 and (predictive_mean_target<10. or predictive_mean_target>90.) and s.steps_remaining>300 and abs(mean-predictive_mean_target)>1e-5:
            for unused in range(2):
                differences=self.grid-mean
                variance=max(1e-9,float(self.bel@(differences*differences)))
                tilt=(predictive_mean_target-mean)/variance
                differences*=tilt
                if not abs(tilt)<.39:np.clip(differences,-40.,40.,out=differences)
                np.exp(differences,out=differences)
                self.bel*=differences
                self.bel/=self.bel.sum()
                mean=float(self.bel@self.grid)
        self._belmean=mean
        # At expiry, use bounded cash cover only on the established
        # bidless shelf; a live penny ask confirms the latest floor reading.
        extra_sell=0.
        if self.marketspread in (2,3) and s.steps_remaining<=90:
            self._floor_age=self._floor_age+1 if b==0 and mean<2. else 0
            if (s.steps_remaining<=40 and self._floor_age>=50 and a==self.marketspread+1 and hi==1
                    and s.yes_inventory<450. and s.cash>600.):
                extra_sell=300.
        elif (self.marketspread==4 and s.steps_remaining<=40 and b==0 and a==5
                and hi==1 and mean<2. and s.yes_inventory<250. and s.cash>300.):
            extra_sell=150.
        floor_risk=self.floor_risk
        floor_quote=floor_risk.observe(s.steps_remaining,b,hi,predictive_mean_target,mean) if floor_risk is not None else None
        ceiling_risk=self.ceiling_risk
        # The mirrored probability is the chance of NO. A live 99c bid
        # supplies the same censor as a live 1c ask in that coordinate.
        ceiling_quote=ceiling_risk.observe(s.steps_remaining,100-a,100-lo,100.-predictive_mean_target,100.-mean) if ceiling_risk is not None else None
        actions=[]
        # Dual threshold probes measure past fair value with minimal edge expenditure.
        cash=max(0,s.cash-.01)
        yes=s.yes_inventory
        if self.probe_arm is not None:probes,cash,yes=self.probe_arm.propose(self,mean,vol,b,a,cash,yes)
        else:probes={}
        # One-sided inventory shortage utility plus FIFO-aware price/quantity control.
        target=min(self.inventorycap,self.targetnotional/max(.02,mean*.01))*min(1.,s.steps_remaining/50.)
        target=max(1.,target);kap=self.kappa
        if self.marketspread==2 and mean>=8.:kap=.002
        if self.dynamic:kap=min(.025,max(0.,(a-1-mean)*.005))
        # Midband sell controllers can use uncovered collateral, so the
        # bids do not need to pay as much for building covered YES stock.
        if (self.marketspread==4 or (self.marketspread==3 and self.controlled)) and 10.<=mean<=90.:
            kap*=.5
        curv=kap/target;gap=target-yes;delta=2*curv*gap
        shadow=(self.replacement*max(0.,.01-mean*.01)*min(1.,s.steps_remaining/self.replacementH)) if (self.marketspread==1 and a>=2) else 0.
        if shadow>0 and yes>0:
            sx=max(.02,(a-1)*.01)
            demands=self.rate*self.meanretail*max(1,s.steps_remaining)/sx
            sd=math.sqrt(math.log1p(math.exp(self.sigma*self.sigma)/max(.001,self.rate*s.steps_remaining)))
            shortage=float(ndtr((math.log(max(1e-50,demands))-sd*sd*.5-math.log(yes))/sd))
            shadow=min(shadow,max(0,sx-mean*.01)*shortage)

        if self.recal is not None:control_bel,control_mean=self.recal.control(self,s)
        else:control_bel,control_mean=self.bel,mean
        pricing23=self.marketspread in (2,3)
        pref=self._pref;control_bel.cumsum(out=pref[1:]);pref[-1]=1.
        moment=self._moment;prod=self._moment_prod
        np.multiply(control_bel,self.grid,out=prod);prod.cumsum(out=moment[1:]);moment[-1]=control_mean
        pref_reader=self._pref_reader;moment_reader=self._moment_reader
        # With the sell controller on, it replaces every native sell order while the mean is
        # inside [10,90], so the sell side is not priced on this step.
        sell_needed=not (self.controlled and .10<=mean/100.<=.90)
        if not fast and not pricing23:
            aBl=pref[self.ilow].tolist();mBl=moment[self.ilow].tolist()
        if ceiling_quote is not None:
            no_mean,no_atoms=ceiling_quote
            def bprice(tick):
                nx=.01*(100-tick)
                loss=0.;arb=0.
                for next_no,weight in no_atoms:
                    if next_no>nx:
                        loss+=weight*(next_no-nx)
                        arb+=weight
                return max(0.,nx-no_mean+loss),loss,arb
        elif fast or pricing23:
            # Native's 301-point grid puts integer ticks at index 3*tick.
            def bprice(tick):
                ix=3*tick;ab=pref_reader[ix];bm=moment_reader[ix]
                lb_=.01*(tick*ab-bm)
                if not lb_>0.:lb_=0.
                gb=.01*(control_mean-tick)+lb_
                if not gb>0.:gb=0.
                return gb,lb_,ab
        else:
            def bprice(tick):
                ab=aBl[tick-1];bm=mBl[tick-1]
                lb_=.01*(tick*ab-bm)
                if not lb_>0.:lb_=0.
                gb=.01*(control_mean-tick)+lb_
                if not gb>0.:gb=0.
                return gb,lb_,ab
        if sell_needed and floor_quote is not None:
            floor_p,floor_atoms=floor_quote
            floor_mean=100.*floor_p
            def sprice(tick):
                px=.01*tick
                ls=0.;arb=0.
                for nextp,weight in floor_atoms:
                    if nextp>px:
                        ls+=weight*(nextp-px)
                        arb+=weight
                return max(0.,.01*(tick-floor_mean)+ls),ls,arb
        elif sell_needed and (fast or pricing23):
            def sprice(tick):
                ix=3*tick+1;as_=1-pref_reader[ix];sm=moment_reader[ix]
                ls_=.01*((control_mean-sm)-tick*as_)
                if not ls_>0.:ls_=0.
                gs=.01*(tick-control_mean)+ls_
                if not gs>0.:gs=0.
                return gs,ls_,as_
        elif sell_needed:
            pHl=pref[self.ihigh].tolist();mHl=moment[self.ihigh].tolist()
            def sprice(tick):
                as_=1-pHl[tick-1];sm=mHl[tick-1]
                ls_=.01*((control_mean-sm)-tick*as_)
                if not ls_>0.:ls_=0.
                gs=.01*(tick-control_mean)+ls_
                if not gs>0.:gs=0.
                return gs,ls_,as_
        actions_main=[]
        if pricing23:
            quote_grid=_price_grid23;quote_value=_price_value23;arb_buy=_price_arb23
        else:
            quote_grid=_best_grid;quote_value=_fast_value;arb_buy=_best_arb_buy
        cover_s2=self.marketspread==2 and extra_sell>0.
        mr=self.meanretail;sg=self.sigma;qgs=self.qgrid_scalar;rate=self.rate
        loc=math.log(mr)-sg*sg*.5;ss=sg*_SQRT2
        qmode=self.marketspread==1 or not self.gatequeue
        skip_arb=(fast or pricing23) and not qmode
        skip_dead_buy=pricing23 and not qmode and gap<=0.
        skip_dead_sell=fast or (pricing23 and not qmode)
        sides=[_BUY,_SELL] if sell_needed else [_BUY]
        for side in sides:
            buy=side==_BUY
            best=(0.,0,0.,None)
            bv=0.
            if buy:
                ticks=range(max(1,min(b+1-extra,math.floor(mean-3*vol)-1)),min(99,max(a-1,math.ceil(mean+vol)+1))+1)
                maxsize=self.maxsize;dep=self.depthbid;rfrom=b+1-extra;px=max(.05,mean*.01)
                if fast:
                    retail_best=max(ticks.start,rfrom)
                    capbest=min(maxsize,cash/(retail_best*.01))
                for tick in ticks:
                    cap=min(maxsize,cash/(tick*.01))
                    if cap<.01:continue
                    r=rate if tick>=rfrom else 0.
                    if skip_dead_buy and not r:continue
                    ahead=max(0,b-tick+1)*dep/(tick*.01) if qmode else 0.
                    gb,lb_,ab=bprice(tick)
                    if skip_arb and not r:
                        if gap<=0 or (pricing23 and ab<=0.) or -lb_+2*curv*gap*ab < -1e-15:continue
                        v,q=arb_buy(cap,lb_,ab,gap,curv,qgs)
                    elif fast and gap<=0. and not qmode and tick>retail_best:
                        # Same q on every tick is dominated by the first retail bid.
                        # Its grid may miss a higher tick's intermediate cash cap.
                        if cap>=capbest:continue
                        q=cap
                        v=quote_value(q,0.,lb_,gb,ab,r,px,mr,sg,gap,curv,shadow,True)
                    else:v,q=quote_grid(cap,ahead,lb_,gb,ab,r,px,mr,sg,gap,curv,shadow,True,qgs,loc,ss)
                    if v>bv:
                        bv=v;best=(v,tick,q,None)
            else:
                ticks=range(max(1,min(b+1,math.floor(mean-vol)-1)),min(99,max(a-1+extra,math.ceil(mean+3*vol)+1))+1)
                cap=min(4500.,yes) if floor_quote is not None else min((self.askmax if self.marketspread==1 or not self.askgate else (self.askmax_spread2 if self.marketspread==2 else self.askmax_other)),yes)
                base_cap=cap
                cap+=extra_sell
                if cap>=.01:
                    dep=self.depthask;rto=a-1+extra
                    if fast:
                        last_sell=min(ticks.stop-1,rto)
                        ticks=range(last_sell,ticks.start-1,-1)
                    for tick in ticks:
                        if cover_s2:
                            cap=base_cap+extra_sell if tick==2 else base_cap
                            if cap<.01:continue
                        r=rate if tick<=rto else 0.
                        if skip_dead_sell and not r:continue
                        ahead=max(0,tick-a+1)*dep/(tick*.01) if qmode else 0.
                        gs,ls_,as_=sprice(tick)
                        if fast and not qmode and tick!=last_sell:
                            # Fixed grid sizes only get better at a higher ask.
                            # Still test the solver's extra off-grid candidate.
                            if gap>=0. or as_<=1e-12:continue
                            oq=(-2*curv*gap*as_-ls_)/(2*curv*as_)
                            if oq<=0.:continue
                            q=min(cap,max(.01,oq))
                            v=quote_value(q,0.,ls_,gs,as_,r,tick*.01,mr,sg,gap,curv,shadow,False)
                        else:v,q=quote_grid(cap,ahead,ls_,gs,as_,r,tick*.01,mr,sg,gap,curv,shadow,False,qgs,loc,ss)
                        if v>bv or (fast and v==bv and bv>0.):
                            bv=v;best=(v,tick,q,None)
            for o in s.own_orders:
                if o.side!=side or o.remaining_quantity<=.011:continue
                tick=o.price_ticks;q=o.remaining_quantity
                inside=(tick>b) if buy else (tick<a)
                partial=q<mem.prevq.get(o.order_id,q)-.005
                if not inside and o.order_id not in mem.front and not partial:continue
                gain,loss,arb=bprice(tick) if buy else sprice(tick)
                r=rate if ((tick>=b) if buy else (tick<=a)) else 0.
                px=max(.05,mean*.01) if buy else tick*.01
                if fast and inside and tick==best[1] and q==best[2] and not qmode:v=best[0]
                else:v=quote_value(q,0.,loss,gain,arb,r,px,mr,sg,gap,curv,shadow,buy)
                if v>0 and v>=best[0]*self.retention:best=(v,tick,q,o.order_id)
            if best[0]>0:
                if side==_SELL and best[3] is None:
                    best=(best[0],best[1],best[2]*(2. if self.marketspread==1 else 1.),best[3])
                actions_main.append((side,*best))
        keep={cid:(side,tick,q) for side,val,tick,q,cid in actions_main if cid is not None}
        for o in s.own_orders:
            oid=o.order_id
            q=o.remaining_quantity
            if q<=.011 and probes.pop((o.side,o.price_ticks),None) is not None:
                keep[oid]=(o.side,o.price_ticks,q)
            elif oid not in keep:actions.append(CancelOrder(oid))
        if fast:self._ret=keep;self._new_start=len(actions)
        cash=max(0,s.cash-.01);yes=s.yes_inventory
        for cid,(side,tick,q) in keep.items():
            if side==_BUY:cash-=q*tick*.01
            else:
                if q<=.011:cash-=q*(1-tick*.01)
                elif cid==self._s2_cash_ask:
                    # Covered shares fill first, so the original uncovered
                    # quantity is still an upper bound on cash-backed shares.
                    cash-=min(q,self._s2_uncovered)*(1-tick*.01)
                yes=max(0.,yes-q)
        # Reserve cheap YES acquisition before fresh probes/main orders.
        if self.stock_arm is not None:cash=self.stock_arm.place(self,s,b,a,mean,shadow,cash,actions)
        if probes:cash,yes=self.probe_arm.place(probes,cash,yes,actions)
        for side,val,tick,q,cid in actions_main:
            if cid is not None:continue
            if side==_BUY:q=min(q,max(0,cash)/(tick*.01))
            elif extra_sell and (not cover_s2 or tick==2):
                q=min(q,max(0.,yes)+min(extra_sell,max(0.,cash)/(1.-tick*.01)))
            else:q=min(q,max(0,yes))
            q=math.floor(q*100)/100
            if q>=.01:
                self.serial+=1;cid='f'+str(self.serial)
                actions.append(PlaceOrder(side,tick,q,cid))
                if side==_BUY:cash-=q*tick*.01
                elif extra_sell and q>yes:
                    if cover_s2:
                        self._s2_cash_ask=cid
                        self._s2_uncovered=q-yes
                    cash-=(q-yes)*(1.-tick*.01)
                    yes=0.
                else:yes-=q
        return actions

    def fast_commit(self,s,b,a,actions):
        mem=self.mem
        priorq=mem.prior_prevq;priorfront=mem.prior_front
        pb=mem.prevbuy;pa=mem.prevask
        pb.fill(0.);pa.fill(0.)
        front=set();pq={};bestbuy=None;bestsell=None
        mb=0;ma=100
        sell_first=False
        for o in s.own_orders:
            oid=o.order_id
            if oid not in self._ret:continue
            side=o.side;tick=o.price_ticks;q=o.remaining_quantity
            pq[oid]=q
            if side==_BUY:
                pb[tick]+=q
                if tick>mb and q>=.1:bestbuy=(oid,side,tick,q);mb=tick
                elif tick==mb and q>=.1:bestbuy=None
                inside=tick>b
            else:
                pa[tick]+=q
                if tick<ma and q>=.1:bestsell=(oid,side,tick,q);ma=tick
                elif tick==ma and q>=.1:bestsell=None
                inside=tick<a
            if inside or q<=.011 or oid in priorfront or q<priorq.get(oid,q)-.005:front.add(oid)
        sell_first=bestsell is not None and bestbuy is None
        for order in actions[self._new_start:]:
            oid=order.client_order_id;side=order.side;tick=order.price_ticks;q=order.quantity
            if side==_BUY:
                pb[tick]+=q
                if oid is not None and q>=.1:
                    if tick>mb:bestbuy=(oid,side,tick,q);mb=tick
                    elif tick==mb:bestbuy=None
                inside=tick>b
            else:
                pa[tick]+=q
                if oid is not None and q>=.1:
                    if tick<ma:bestsell=(oid,side,tick,q);ma=tick;sell_first=False
                    elif tick==ma:bestsell=None
                inside=tick<a
            if oid is not None:
                if inside and not oid.startswith('h'):front.add(oid)
                pq[oid]=q
        last={}
        if sell_first and bestsell is not None:
            oid,side,tick,q=bestsell;last[oid]=(side,tick,q)
        if bestbuy is not None:
            oid,side,tick,q=bestbuy;last[oid]=(side,tick,q)
        if not sell_first and bestsell is not None:
            oid,side,tick,q=bestsell;last[oid]=(side,tick,q)
        mem.flowlast=last;mem.pfront=mem.front=front;mem.prevq=pq;mem.pb=b;mem.pa=a


# ============================ The native player: arms ============================
class FlowLearner:
    """Arm flow_learning. The learned retail flow: a posterior over 11 rates and 15 mean sizes,
    updated from how much of each certified main order (OrderMemory.flowlast) retail filled.
    Reads: the OrderMemory's flowlast, pfront, pb and pa, the core's belief mean, the state's own
    orders. May change: the core's retail model (core.rate, core.meanretail, core.sigma), before
    the core proposes; compound on FLOW_COMPOUND spreads, where it turns the rate and size into
    the chance and size of a step's compound order. Off, the core keeps .125, 4.5 and 1.2."""
    fast=False
    adapt_compound=0
    adapt_r=1
    adapt_mean=1
    adapt_target=0
    adapt_power=1.
    def __init__(self,compound):
        if compound:self.adapt_compound=3
        self._likcache={};self._zerocache=None
        self.rs=np.linspace(.077,.176,11)
        self.ms=np.linspace(2.64,6.336,15)
        self.weight=np.ones((len(self.rs),len(self.ms)))/(len(self.rs)*len(self.ms))
        self._weightflat=self.weight.ravel()
        self._rsflat=np.repeat(self.rs,len(self.ms))
        self._msflat=np.tile(self.ms,len(self.rs))
        ns=np.arange(1,5)
        self.sigs=np.sqrt(np.log(1+(math.exp(1.2**2)-1)/ns))
        self.mus=np.log(ns[:,None]*self.ms[None,:])-.5*self.sigs[:,None]**2
        self.p0=np.exp(-self.rs)
        self.pois=np.array([self.p0*self.rs**int(n)/math.factorial(int(n)) for n in ns]).T
        self.obs=0;self.targetbase=NativeCore.targetnotional;self.ratebase=NativeCore.rate;self.meanbase=NativeCore.meanretail
        if compound:
            self._cpany=np.repeat(1.-self.p0,len(self.ms))
            self._cm1=(self.rs[:,None]*self.ms[None,:]).ravel()
            self._cm2=((self.rs[:,None]*math.exp(1.44)+self.rs[:,None]**2)*self.ms[None,:]**2).ravel()
    def observe(self,v,cap):
        if cap<.01:return
        zero=v<.001
        cache=self._zerocache if zero and self._zerocache is not None else self._likcache
        if cache is self._likcache:
            key=(0. if zero else v,cap,self.adapt_power)
        else:
            key=round(math.log(cap)*6.25)
        likelihood=cache.get(key)
        if likelihood is None:
            try:
                sigc=self._sigc;p0c=self._p0c
            except AttributeError:
                sigc=self._sigc=self.sigs[:,None];p0c=self._p0c=self.p0[:,None]
            lc=math.log(cap) if cache is self._likcache else key*.16;_e=math.erfc;_s=_SQRTH
            F=np.interp((lc-self.mus)/sigc,_TAIL_Z,_TAIL_CDF)
            norm=p0c+self.pois@F
            if v<.001:
                lik=p0c/norm
            else:
                z=(math.log(max(v,1e-9))-self.mus)/sigc
                f=np.exp(-.5*z*z)/(v*sigc*math.sqrt(2*math.pi))
                lik=(self.pois@f)/norm
            likelihood=np.maximum(lik,1e-100)
            if self.adapt_power!=1:likelihood=likelihood**self.adapt_power
            if len(cache)>=256:cache.clear()
            cache[key]=likelihood
        self.weight*=likelihood
        self.weight/=self.weight.sum()
        self.obs+=1
    def update(self,core,mem,s,active=None):
        oldobs=self.obs
        if mem.flowlast:
            if self.fast:
                lb=0.;ub=1.;live={}
                for o in s.own_orders:
                    live[o.order_id]=o
                    px=o.price_ticks*.01
                    if o.side==_BUY:
                        if px>lb:lb=px
                    elif px<ub:ub=px
            elif active is not None:
                live=active
                lb=0;ub=100
                for side,tick,q in live.values():
                    if side is _BUY:
                        if tick>lb:lb=tick
                    elif tick<ub:ub=tick
                lb*=.01;ub*=.01
            else:
                live={o.order_id:o for o in s.own_orders}
                lb=max([o.price_ticks*.01 for o in s.own_orders if o.side==_BUY]+[0.])
                ub=min([o.price_ticks*.01 for o in s.own_orders if o.side==_SELL]+[1.])
            prior=core._belmean*.01 if core.bel is not None else .5
            p=max(lb,min(ub,prior))
            for cid,(side,tick,q) in mem.flowlast.items():
                if q<.1 or cid not in live:continue
                if side==_BUY and (tick<mem.pb or (tick==mem.pb and cid not in mem.pfront)):continue
                if side==_SELL and (tick>mem.pa or (tick==mem.pa and cid not in mem.pfront)):continue
                order=live[cid]
                rem=order.remaining_quantity if active is None else order[2]
                conv=max(.05,p) if side==_BUY else tick*.01
                self.observe(max(0,q-rem)*conv,q*conv)
        if self.fast and self.obs==oldobs and s.step>0:return
        if self.adapt_compound!=3:
            core.rate=float(self._weightflat@self._rsflat) if self.adapt_r else self.ratebase
            core.meanretail=float(self._weightflat@self._msflat) if self.adapt_mean else self.meanbase
        core.sigma=1.2
        if self.adapt_compound:
            if self.adapt_compound==3:
                pany=float(self._weightflat@self._cpany)
                m1=float(self._weightflat@self._cm1)/pany
                m2=float(self._weightflat@self._cm2)/pany
            elif self.adapt_compound==2:
                pany=float(np.sum(self.weight*(1-self.p0[:,None])))
                m1=float(np.sum(self.weight*self.rs[:,None]*self.ms[None,:]))/pany
                m2=float(np.sum(self.weight*(self.rs[:,None]*math.exp(1.44)+self.rs[:,None]**2)*self.ms[None,:]**2))/pany
            else:
                rawr=core.rate;rawm=core.meanretail;pany=1-math.exp(-rawr)
                m1=rawr*rawm/pany;m2=(rawr*math.exp(1.44)+rawr**2)*rawm**2/pany
            core.rate=pany;core.meanretail=m1;core.sigma=math.sqrt(max(.01,math.log(m2/(m1*m1))))
        if self.adapt_target:
            scale=float(np.sum(self.weight*self.rs[:,None]*self.ms[None,:]))/(.125*4.5)
            core.targetnotional=self.targetbase*scale;core.inventorycap=3000.*scale

class FastFlowLearner(FlowLearner):
    """Spread-3 flow: nearest log-cap lookup for the dominant no-fill observations."""
    def __init__(self,compound):
        super().__init__(compound)
        lc=-4.625+.025*np.arange(550,dtype=float)
        F=np.interp((lc[:,None,None]-self.mus)/self.sigs[:,None],_TAIL_Z,_TAIL_CDF)
        self.nofill=self.p0[:,None]/(self.p0[:,None]+self.pois@F)
        self.estimate=(float(self._weightflat@self._rsflat),float(self._weightflat@self._msflat))
    def observe(self,v,cap):
        if cap<.01:return
        if v>=.001 or cap>8e3:
            super().observe(v,cap)
            return
        self.weight*=self.nofill[int((math.log(cap)+4.625)*40.+.5)]
        self.weight/=self.weight.sum()
        self.obs+=1
    def update(self,core,mem,s):
        n=self.obs
        live=None;px=None
        for cid,(side,tick,q) in mem.flowlast.items():
            if side==_BUY:
                if tick<mem.pb or (tick==mem.pb and cid not in mem.pfront):continue
            elif tick>mem.pa or (tick==mem.pa and cid not in mem.pfront):continue
            if live is None:live={o.order_id:o for o in s.own_orders}
            o=live.get(cid)
            if o is None:continue
            if side==_BUY:
                if px is None:
                    lb=0.;ub=1.
                    for order in s.own_orders:
                        y=order.price_ticks*.01
                        if order.side==_BUY:
                            if y>lb:lb=y
                        elif y<ub:ub=y
                    prior=core._belmean*.01 if core.bel is not None else .5
                    px=max(.05,lb,min(ub,prior))
                conv=px
            else:conv=tick*.01
            self.observe(max(0,q-o.remaining_quantity)*conv,q*conv)
        if n!=self.obs:
            self.estimate=(float(self._weightflat@self._rsflat),float(self._weightflat@self._msflat))
        core.rate,core.meanretail=self.estimate

class NativeProbes:
    """Arm probes. Tiny 0.01 buys and sells on every tick within about half a step's move of the
    belief's mean (one move above it); their fills and non-fills are what the core's belief update
    reads, and the simulator's work on them is about a fifth of a seed's CPU. Reads: the belief's
    mean and move, the touches, the cash and YES left. May change: proposes its probes before the
    core prices its main orders (their cash and YES come off first) and places them after the
    stock order, within the cash left; the core keeps a resting probe that is proposed again."""
    _tables=None

    def __init__(self):
        self.plans={}
        tables=NativeProbes._tables
        if tables is None:
            upx=tuple(t*.01*.01 for t in range(100))
            factor=tuple(1-t*.01 for t in range(100))
            uncovered=tuple(.01*w for w in factor)
            bid=(None,)+tuple((PlaceOrder(_BUY,t,.01),upx[t],0.) for t in range(1,100))
            ask=(None,)+tuple((PlaceOrder(_SELL,t,.01),uncovered[t],factor[t]) for t in range(1,100))
            bare=tuple(x+y for x,y in zip(upx,uncovered))
            tables=NativeProbes._tables=(bid,ask,upx,factor,bare)
        self.tables=tables

    def propose(self,core,mean,vol,b,a,cash,yes):
        if cash<=1:return {},cash,yes
        lo=max(1,math.floor(mean-.5*vol))
        hi=min(99,math.ceil(mean+(1. if core.marketspread>=2 else .5)*vol))
        if lo>hi:return {},cash,yes
        key=(lo,hi,b,a)
        plans=self.plans
        plan=plans.get(key)
        if plan is None:
            bid,ask,upx,factor,bare=self.tables
            orders={}
            for t in range(lo,hi+1):
                if t<=b:orders[(_BUY,t)]=bid[t]
                elif t>=a:orders[(_SELL,t)]=ask[t]
                else:
                    orders[(_BUY,t)]=bid[t]
                    orders[(_SELL,t)]=ask[t]
            plan=plans[key]=(orders,upx[lo:hi+1],bare[lo:hi+1],factor[lo:hi+1])
        orders,prices,naked,factors=plan
        # Preserve each subtraction's original floating-point order.
        if yes>=1.:
            for x in prices:
                yes-=.01
                cash-=x
        elif yes==0.:
            for x in naked:cash-=x
        else:
            for x,w in zip(prices,factors):
                cov=min(yes,.01)
                yes-=cov
                cash-=x+w*(.01-cov)
        return orders.copy(),cash,yes

    def place(self,probes,cash,yes,actions):
        add=actions.append
        # At most 198 penny probes: with $2 left each passes the cash test.
        if cash>2. and yes>=1.:
            for order,cost,factor in probes.values():
                add(order)
                if factor:yes-=.01
                else:cash-=cost
        elif cash>2. and yes==0.:
            for order,cost,factor in probes.values():
                add(order)
                cash-=cost
        else:
            for order,cost,factor in probes.values():
                need=max(0,.01-yes)*factor if factor else cost
                if need>cash and math.floor((.01*(max(0,cash)/max(need,1e-10)))*100)<1:continue
                add(order)
                if factor:
                    cov=min(yes,.01)
                    yes-=cov
                    cash-=(.01-cov)*factor
                else:cash-=cost
        return cash,yes


_DP_STR=[]
_DP_COLV={}
def _dp_at(H,c):
    col=_DP_COLV.get(c)
    if col is None:
        if not _DP_STR:_DP_STR.append("|".join(sorted(_DP_CODE,key=len)))
        table=_DP_CODE
        words=re.findall(_DP_STR[0],bin(int(_DP_COLUMNS[c],16))[3:])
        col=_DP_COLV[c]=list(accumulate([table[w] for w in words[:2001]]))
    return round(col[H]*(25/1024),6)

class YesStock:
    """Arm yes_stock. One bid at one tick for YES while the YES held is below a target, so the
    core's asks at low prices are covered: the target is a day of retail demand at the ask, and
    below a price of 0.015 with the competitor's bid at 8 or less it comes from the DP table of
    inventory targets (_dp_at). Reads: the core's retail model and belief mean, the state, the
    cash left after the kept orders. May change: appends its order (id 'h' + the core's serial)
    and returns the cash left."""
    hedge_target=20000
    hedge_target_spread1=50000
    hedge_factor=1.
    hedge_cap=30000.
    replacementgate=True
    def __init__(self,s3_anchor=False):
        self.s3_anchor=s3_anchor
    def place(self,core,s,b,a,mean,shadow,cash,actions):
        if core.marketspread==3 and mean>20:return cash
        # A fresh 1c bid filling behind competitor bid 1 clears that
        # incumbent, which refills the next step as an ask at 3c. Blend
        # the consequent 2c selling shelf with today's selling shelf.
        # Leave the established buffer unchanged late in the market.
        shelf=core.marketspread==2 and b==1 and a>=3 and mean<2.9 and s.steps_remaining>=350
        target=min(self.hedge_target_spread1 if core.marketspread==1 else self.hedge_target,core.rate*core.meanretail/((.01 + .005*(a-1)) if shelf else max(.02,(a-1)*.01))*s.steps_remaining*self.hedge_factor)
        ep=mean*.01
        ask=(1. + .5*(a-1)) if shelf else max(2,a-1)
        if core.marketspread>=2 and b<=8 and ep<.015:
            H=max(0,min(2000,int(min(2000,s.steps_remaining-1)*core.rate/.125)))
            cost=(2. if core.marketspread>=3 else 1.)/ask
            j=bisect_left(DP_COSTS,cost)
            if j<=0:stock=_dp_at(H,0)
            elif j>=len(DP_COSTS):stock=_dp_at(H,52)
            else:
                w=(cost-DP_COSTS[j-1])/(DP_COSTS[j]-DP_COSTS[j-1])
                stock=_dp_at(H,j-1)*(1-w)+_dp_at(H,j)*w
            target=min(50000. if core.marketspread==1 else 30000.,(3. if core.marketspread==1 else 2.)*((.75 if core.marketspread==1 else .7)+.5*ep/(.02 if core.marketspread==1 else .015))*stock*core.meanretail/(ask*.01))
        if self.s3_anchor and core.marketspread==3 and 1<=b<=8 and ask>3 and mean<2.9:
            # A 1c stock fill normally clears the rival bids and restores
            # the 4c ask. Blend in the resulting 3c sale-capacity target.
            if ep<.015:
                w=(2./3.-DP_COSTS[34])/(DP_COSTS[35]-DP_COSTS[34])
                floorstock=_dp_at(H,34)*(1-w)+_dp_at(H,35)*w
                alternate=min(30000.,2.*(.7+.5*ep/.015)*floorstock*core.meanretail/.03)
            else:alternate=min(self.hedge_target,core.rate*core.meanretail*s.steps_remaining/.03)
            target+=.5*(alternate-target)
        hedge_cap=self.hedge_cap
        if core.marketspread==2:
            # Accumulate more of the buffer around a crossing of the 1c bid,
            # where replenishing is usually cheaper than after another fall.
            if mean<.75:target*=.82
            elif mean<2.9:
                hedge_cap=44000.
                target=min(hedge_cap,target*1.22)
        if (a>=3 or (self.replacementgate and core.marketspread==1 and a>=2 and shadow>0)) and s.yes_inventory<target:
            q=min(target-s.yes_inventory,hedge_cap,max(0,cash)*.99/.01)
            q=math.floor(q*100)/100
            if q>=.01:
                cid=core.new_id('h')
                actions.append(PlaceOrder(_BUY,1,q,cid))
                cash-=q*.01
        return cash

class Recalibration:
    """Arm recalibration. A fixed net (25 features, two tanh layers, 19 bins) that reweights the
    core's belief by its quantile, half and half with the belief, while the mean is in [10,90];
    the core prices from the result. Reads: the core's belief, grid, marketspread, retail model and
    queue depths, the state, its own last 16 steps. May change: nothing in the core; returns the
    belief and mean the core prices with. Off, the core prices from its own belief."""
    def __init__(self):
        self._cal_history=[]
        self._logits19=np.empty(20)
        self._logits=self._logits19[:19]
        self._half=np.empty(301)
    def features(self,st,s,mean=None):
        bel=st.bel;grid=st.grid
        if mean is None:mean=float(bel@grid)
        d=grid-mean;d2=d**2
        var=max(.0001,float(bel.dot(d2)))
        sd=math.sqrt(var)
        np.multiply(d2,d,out=d)
        skew=float(bel.dot(d))/(sd**3)
        cdf=bel.cumsum()
        quant=np.interp(_CAL_Q,cdf,grid)
        hist=self._cal_history
        b=s.competitor_best_bid_ticks or 0;a=s.competitor_best_ask_ticks or 100
        if hist:prior,oldb,olda=hist[-1]
        else:prior,oldb,olda=mean,b,a
        n=len(hist)
        lag4=hist[-4][0] if n>=4 else prior
        lag16=hist[-16][0] if n>=16 else prior
        H=s.steps_remaining
        features=np.array([mean*.01,H/2000.,math.log(max(1,H))/8.,sd*.2,skew*.1,
            (quant[0]-mean)*.2,(quant[1]-mean)*.2,(quant[2]-mean)*.2,
            (b-mean)*.1,(a-mean)*.1,st.marketspread*.25,
            (b-oldb)*.2,(a-olda)*.2,(mean-prior)*.5,(mean-lag4)*.25,(mean-lag16)*.1,
            st.rate/.125,st.meanretail/4.5,st.depthbid/48.,st.depthask/48.,
            math.log1p(max(0,s.buy_filled_quantity))*.2,math.log1p(max(0,s.sell_filled_quantity))*.2,
            math.log1p(max(0,s.yes_inventory))*.1,float(bel[0]),float(bel[-1])],dtype=float)
        hist.append((mean,b,a))
        if len(hist)>16:del hist[0]
        return features,cdf,mean
    def control(self,st,s):
        native_mean=st._belmean
        if not 10.<=native_mean<=90.:
            hist=self._cal_history
            hist.append((native_mean,s.competitor_best_bid_ticks or 0,s.competitor_best_ask_ticks or 100))
            if len(hist)>16:del hist[0]
            return st.bel,native_mean
        f,cdf,native_mean=self.features(st,s,native_mean)
        np.maximum(f,_CAL_RAWLO,out=f);np.minimum(f,_CAL_RAWHI,out=f)
        z=_CAL_ARAW.dot(f);z+=_CAL_ABRAW;np.tanh(z,out=z)
        z=_CAL_B.dot(z);z+=_CAL_BB;np.tanh(z,out=z)
        logits=self._logits
        _CAL_C.dot(z,out=logits);logits+=_CAL_CB
        np.maximum(logits,-3.,out=logits);np.minimum(logits,3.,out=logits)
        half=self._half
        np.multiply(.5,st.bel,out=half)
        cdf-=half;cdf*=19;bins=cdf.astype(int)
        # Roundoff can put the last CDF bin at 19 rather than 18.
        np.exp(logits,out=logits)
        extended=self._logits19;extended[19]=extended[18]
        calibrated=extended[bins];calibrated*=st.bel
        denom=max(1e-100,float(calibrated.sum()))
        calibrated*=.5;calibrated/=denom;calibrated+=half
        return calibrated,float(calibrated.dot(st.grid))


# Small shared tables for the controller's reservation and tick math.
_SC_PRICE=None
def _load_sell_tables():
    global _SC_PRICE,_SC_SELL,_SC_PENNY_BUY3,_SC_PENNY_SELL
    if _SC_PRICE is None:
        _SC_PRICE=tuple(t/100. for t in range(101))
        _SC_SELL=tuple(1.-x for x in _SC_PRICE)
        _SC_PENNY_BUY3=tuple(round(.01*t/100.,10) for t in range(101))
        _SC_PENNY_SELL=tuple(round(.01*x,10) for x in _SC_SELL)

def _fast_sell4(state,actions,core,mem,ledger,serial):
    b=state.competitor_best_bid_ticks or 0
    a=state.competitor_best_ask_ticks or 100;t=min(99,a-1)
    if t<=b or a<=1:return actions,serial
    p=core._sell_grid100
    w=core.bel
    work=core._sell4_work
    if work is None:
        work=core._sell4_work=(np.empty_like(w),np.empty_like(w),np.empty_like(w),tuple(i-1 for i in core.ihigh.tolist()))
    W,PW,prod,offsets=work
    w.cumsum(out=W);np.multiply(p,w,out=prod);prod.cumsum(out=PW)
    allw=float(W[-1]);allpw=float(PW[-1])
    prices=_SC_PRICE;reqs=_SC_SELL
    penny_buy=_SC_PENNY_BUY3;penny_sell=_SC_PENNY_SELL
    def moments(tick):
        px=prices[tick];i=offsets[tick-1]
        aw=float(W[i]);apw=float(PW[i])
        return px*aw-apw,(allpw-apw)-px*(allw-aw)
    # The core already chose the protected bids/stock/probes. Simulate all of
    # those reservations before assessing feasible retained or fresh asks.
    active=ledger.active;named=ledger.named
    free=state.free_cash;yes=state.yes_inventory-sum(v[2] for v in named.values())
    old=[]
    for action in actions:
        if type(action) is CancelOrder:
            oid=action.order_id;side,tick,q=active[oid]
            if side is _BUY:
                free+=penny_buy[tick] if q==.01 else round(prices[tick]*q,10)
            else:
                cov=named[oid][2]
                if q==cov:rc=0.
                elif q==.01 and cov==0.:rc=penny_sell[tick]
                else:rc=round((q-cov)*reqs[tick],10)
                free+=rc;yes+=cov
                if q>.011 and (tick<a or (tick==a and (oid in mem.prior_front or q<mem.prior_prevq.get(oid,q)-.005))):
                    old.append((oid,tick,q,rc,cov))
        elif type(action) is PlaceOrder:
            q=action.quantity;tick=action.price_ticks
            if action.side is _BUY:
                free-=penny_buy[tick] if q==.01 else round(prices[tick]*q,10)
            elif yes>=q:yes-=q
            elif q==.01 and yes<=0.:free-=penny_sell[tick]
            else:
                cov=min(max(0.,yes),q);yes-=cov;free-=round(reqs[tick]*(q-cov),10)
        else:return actions,serial
    free=max(0.,free-.01);yes=max(0.,yes)
    m=core.meanretail;sg=core.sigma;rate=core.rate;half=.5*sg*sg
    half_value=sg*sg*.5
    def value(q,px,gain,loss):
        # Same a=0, need2=False branch of _moments; no tuple or variance.
        if q<=0.:return -q*loss+rate*gain*0.
        scale=m/px
        z=(math.log(q/scale)+half_value)/sg
        tail=.5*math.erfc(z*.7071067811865475)
        e1=scale*.5*math.erfc(-(z-sg)*.7071067811865475)+q*tail
        return -q*loss+rate*gain*e1
    g,l=moments(t);px=prices[t]
    bestid=None;bestv=0.;bestq=0.
    if g>0. and l<rate*g:
        cap=yes+free/reqs[t]
        prob=l/(rate*g)
        q=cap if prob<=0. else min(cap,(m/px)*math.exp(-sg*ndtri(prob)-half))
        q=math.floor(q*100.)/100.
        if q>=.01:
            bestv=value(q,px,g,l);bestq=q
    for oid,tick,q,rc,cov in old:
        if rc>free+1e-9 or cov>yes+1e-9:continue
        gain,loss=(g,l) if tick==t else moments(tick)
        v=value(q,prices[tick],gain,loss)
        if v>0. and v>=bestv:
            bestv=v;bestid=oid
    # NativeCore emits every cancellation before any new placement.
    if bestid is not None:
        return [act for act in actions if type(act) is not CancelOrder or act.order_id!=bestid],serial
    if bestq>=.01 and bestv>0.:
        serial+=1;actions.append(PlaceOrder(_SELL,t,bestq,'oracle-'+str(serial)))
    return actions,serial

class SellController:
    """Native ask overlay, accounting for previously selected bids and probes."""
    def __init__(self,core):
        _load_sell_tables()
        self.optimizer=MyopicController()
        self._grid100=core.grid/100.
        core._sell_grid100=self._grid100
        core._sell4_work=None
        self.fast=False
    def step(self,state,proposed,core,mem,named):
        plan=_plan_spread3 if core.marketspread==3 else _plan_causal_fast
        mainstate,ctx,cp,npb=plan(state,proposed,core,mem,named,self._grid100)
        return finalize_protected(cp,npb,self.optimizer.on_step(mainstate,ctx))

class MyopicController:
    def __init__(self):
        self.serial=0
        self._kt=None
        self._wc=None;self._pwc=None
    def _single_level(self,s,old,sensors,rr,mean,sg,t):
        """Exact specialization when no eligible ask rests at a second tick."""
        cb=s.competitor_best_bid_ticks or 0
        if t<=cb and not old:return [CancelOrder(o.order_id) for o in s.own_orders]
        if t<=0:return None
        W=self._wc;P=self._pwc;kt=self._kt[1]
        k=kt[t];x=t/100.
        wk=float(W[k]);pk=float(P[k])
        gain=x*wk-pk
        loss=(float(P[kt[100]])-pk)-x*(float(W[kt[100]])-wk)
        if not (gain>0. and loss>0.):return None
        half=.5*sg*sg
        if not rr>0.:return None
        cash=max(0.,s.cash-.01);yes=s.yes_inventory
        scale=mean/x
        weights=[]
        for sensor in sensors:
            T=sensor[3]
            if T<=t and kt[T]>0:
                top=kt[T]
                edge=x*float(W[top])-float(P[top])
                weights.append((T,sensor[5],sensor[4]*(T/t)*(edge/gain)))
        ahead=0.
        for T,seq,z in weights:ahead+=z
        if ahead<=0.:ahead=0.
        cap=max(0.,yes)+cash/(1-x)
        bestv=0.;fresh=0.;keep=None
        def capped(u):
            if u<=0.:return 0.
            z=(math.log((u if u>=1e-200 else 1e-200)/scale)+half)/sg
            return scale*_ndtr1(z-sg)+u*_ndtr1(-z)
        if t>cb and cap>=.01:
            prob=loss/(rr*gain)
            q=0.
            if prob<=0.:q=cap
            elif prob<1.:
                root0=scale*math.exp(-sg*ndtri(prob)-half)
                rlo=root0-ahead-1e-8;rhi=root0-ahead+1e-8
                if rhi>0.:
                    if rlo<=0.:
                        g0=gain*_ndtr1(-((math.log(max(1e-200,ahead*x)/mean)+half)/sg))
                    else:g0=0.
                    if rlo>0. or -loss+rr*g0>0.:
                        icap=math.floor(cap*100.)
                        ilo=min(icap,max(0,math.floor(rlo*100.)))
                        ihi=min(icap,max(0,math.floor(rhi*100.)))
                        while ilo<ihi:
                            imid=(ilo+ihi+1)//2
                            u=max(1e-200,(ahead+imid/100.)*x)
                            d=gain*_ndtr1(-((math.log(u/mean)+half)/sg))
                            if -loss+rr*d>=0.:ilo=imid
                            else:ihi=imid-1
                        q=(ilo+.5)/100.
            q=math.floor(q*100.)/100.
            if q>=.01:
                e=capped(ahead+q)-capped(ahead)
                v=-q*loss+rr*(gain*(e if e>0. else 0.))
                if v>1e-12:fresh=q;bestv=v
        for o in old:
            if o[6]>cash+1e-9 or o[7]>yes+1e-9:continue
            ahead=0.
            for T,seq,z in weights:
                if T<t or seq<o[5]:ahead+=z
            if ahead<=0.:ahead=0.
            q=o[4]
            e=capped(ahead+q)-capped(ahead)
            v=-q*loss+rr*(gain*(e if e>0. else 0.))
            if v>0. and v>=bestv:bestv=v;keep=o[0]
        actions=[CancelOrder(o.order_id) for o in s.own_orders if o.order_id!=keep]
        if keep is None and fresh>=.01:
            q=math.floor(min(fresh,max(0.,cap))*100.)/100.
            if math.isfinite(q) and q>=.01:
                self.serial+=1
                actions.append(PlaceOrder(_SELL,t,q,'oracle-'+str(self.serial)))
                assert cash-max(0.,q-min(max(0.,yes),q))*(1-x)>=-1e-8
        return actions

    def on_step(self,s,ctx):
        old,sensors,rr,mean,sigma,pp,ww=ctx
        # The atoms are the native's fixed grid: checked once. kt[t]: atoms i with
        # t/100 >= pp[i]-1e-9 form the prefix [0,kt[t]) (pp increasing).
        kt=self._kt
        if kt is None or kt[0]!=len(pp):
            assert np.isfinite(pp).all() and np.min(pp)>=0. and np.max(pp)<=1.
            ppm=(pp-1e-9).tolist()
            kt=(len(pp),[_bisect_right(ppm,t/100.) for t in range(101)])
            self._kt=kt
            self._wc=np.zeros(len(pp)+1);self._pwc=np.zeros(len(pp)+1)
            self._wcv=self._wc[1:];self._pwcv=self._pwc[1:];self._prod=np.empty(len(pp))
        kt=kt[1]
        # Running sums of the weights and of price times weight: a tick's loss and a run of
        # atoms' edge weight are differences of them.
        ww.cumsum(out=self._wcv)
        prod=self._prod
        np.multiply(pp,ww,out=prod);prod.cumsum(out=self._pwcv)
        t=(s.competitor_best_ask_ticks or 100)-1
        if all(o[3]==t for o in old):
            one=self._single_level(s,old,sensors,rr,mean,sigma,t)
            if one is not None:return one
        wc=self._wc;pwc=self._pwc;bounds=kt
        w100=float(wc[bounds[100]]);pw100=float(pwc[bounds[100]])
        moments={0:(0.,0.),100:(w100,pw100)}
        def at(t):
            term=moments.get(t)
            if term is None:
                i=bounds[t]
                term=moments[t]=(float(wc[i]),float(pwc[i]))
            return term
        half=.5*sigma*sigma
        kept=[];fresh=[];cash=max(0.,s.cash-.01);yes=s.yes_inventory
        name=_SELL.value
        cb=s.competitor_best_bid_ticks or 0;ca=s.competitor_best_ask_ticks or 100
        inside=min(99,ca-1)
        tlist={o[3] for o in old}
        if ca>1:tlist.add(inside)
        if not tlist:return [CancelOrder(o.order_id) for o in s.own_orders]
        # Atoms above a tick's price lose (p-x) each.
        loss={t:(pw100-pw)-_SC_PRICE[t]*(w100-w) for t in tlist for w,pw in (at(t),)}
        _groups={}
        def groups(t,other=None,seq=10**30):
            key=(t,other,seq)
            G=_groups.get(key)
            if G is not None:return G
            k=t;x=_SC_PRICE[t]
            if k<=0:G=[]
            else:
                cut=0 if other is None or other[3]>=t else other[3]
                ends=[cut,k] if 0<cut<k else [k]
                G=[];start=0;sw=spw=0.
                for end in ends:
                    ew,epw=at(end)
                    wg=x*(ew-sw)-(epw-spw)
                    if wg!=0.:
                        aa=(other[4]*(other[3]/t)) if other is not None and start<cut else 0.
                        for sensor in sensors:
                            T=sensor[3]
                            if T>t or (T==t and sensor[5]>=seq):continue
                            top=min(end,T)
                            if top<=start:continue
                            tw,tpw=at(top)
                            edge=x*(tw-sw)-(tpw-spw)
                            aa+=sensor[4]*(T/t)*(edge/wg)
                        G.append((aa if aa>0. else 0.,wg))
                    start=end;sw=ew;spw=epw
            _groups[key]=G
            return G
        def dsum(G,q,xr):
            tot=0.
            for a,wg in G:
                u=(a+q)*xr
                if not u>=1e-200:u=1e-200
                tot+=wg*_ndtr1(-((math.log(u/mean)+half)/sigma))
            return tot
        def dsum2(G,q,xr,_pdf=0.3989422804014327):
            # The sum dsum and its derivative in q.
            tot=0.;der=0.
            for a,wg in G:
                u=(a+q)*xr
                if not u>=1e-200:
                    tot+=wg*_ndtr1(-((math.log(1e-200/mean)+half)/sigma))
                    continue
                z=(math.log(u/mean)+half)/sigma
                tot+=wg*_ndtr1(-z)
                der-=wg*_pdf*math.exp(-.5*z*z)/(sigma*(a+q))
            return tot,der
        def capped(u,scale):
            if u>0.:
                z=(math.log((u if u>=1e-200 else 1e-200)/scale)+half)/sigma
                return scale*_ndtr1(z-sigma)+u*_ndtr1(-z)
            return 0.
        def esum(G,q,xr):
            scale=mean/xr;tot=0.
            for a,wg in G:
                e=capped(a+q,scale)-capped(a,scale)
                tot+=wg*(e if e>0. else 0.)
            return tot
        _val_memo={}
        def val(o,other=None):
            key=(o,other)
            memo=_val_memo.get(key)
            if memo is not None:return memo
            t=o[3];q=o[4];seq=o[5]
            G=groups(t,other,seq)
            result=float(-q*loss[t]+rr*esum(G,q,t/100.))
            _val_memo[key]=result
            return result
        def reserve(o):
            if o[0] is not None:return o[6],o[7]
            return max(0.,o[4]-yes)*_SC_SELL[o[3]],min(yes,o[4])
        def feasible(book):
            rc=0.;ry=0.
            for o in book:
                if o[0] is not None:rc+=o[6];ry+=o[7]
                else:
                    oc,oy=reserve(o);rc+=oc;ry+=oy
            return rc<=cash+1e-9 and ry<=yes+1e-9
        def best_fresh(parent=None):
            t=inside
            if not cb<t<ca or (parent is not None and parent[3]>=t):return None,0.
            rc0,ry0=(0.,0.) if parent is None else reserve(parent)
            available=max(0.,cash-rc0)
            base=max(0.,yes-ry0)
            cap=base+available/_SC_SELL[t]
            if cap<0.:cap=0.
            if not cap>=.01:return None,0.
            xr=t/100.
            G=groups(t,parent)
            singleton=len(G)==1
            if singleton:
                aa0,gain=G[0]
                direct=loss[t]>0. and gain>0. and aa0>=0.
            else:
                gain=sum(wg for aa,wg in G)
                direct=(loss[t]>0. and gain>0. and all(wg>=0. and aa>=0. for aa,wg in G))
            if direct:
                prob=loss[t]/(rr*gain)
                if prob>=1.:return None,0.
                if prob<=0.:q=cap
                else:
                    root0=(mean/xr)*math.exp(-sigma*ndtri(prob)-half)
                    if singleton:aa_min=aa0;aa_max=aa0
                    else:aa_min=min(aa for aa,wg in G);aa_max=max(aa for aa,wg in G)
                    rlo=root0-aa_max-1e-8;rhi=root0-aa_min+1e-8
                    if rhi<=0.:return None,0.
                    if rlo<=0. and not -loss[t]+rr*dsum(G,0.,xr)>0.:return None,0.
                    icap=math.floor(cap*100.)
                    ilo=min(icap,max(0,math.floor(rlo*100.)))
                    ihi=min(icap,max(0,math.floor(rhi*100.)))
                    while ilo<ihi:
                        imid=(ilo+ihi+1)//2
                        if -loss[t]+rr*dsum(G,imid/100.,xr)>=0.:ilo=imid
                        else:ihi=imid-1
                    q=(ilo+.5)/100.
            else:
                if not -loss[t]+rr*dsum(G,0.,xr)>0.:return None,0.
                q=cap
                dc=-loss[t]+rr*dsum(G,cap,xr)
                if dc<0. and len(G)==1 and loss[t]>0.:
                    aa,wg=G[0]
                    prob=loss[t]/(rr*wg)
                    if 0.<prob<1.:
                        q=min(cap,max(0.,(mean/xr)*math.exp(-sigma*ndtri(prob)-half)-aa))
                    else:q=0.
                elif dc<0.:
                    # The derivative falls in q; its root is found by Newton steps kept inside
                    # the bracket [lo,hi] (a step leaving it halves the bracket instead).
                    lo=0.;hi=cap;d0=-loss[t]+rr*dsum(G,0.,xr)
                    q=cap*d0/(d0-dc)
                    gain=sum(wg for aa,wg in G)
                    if loss[t]>0. and gain>0. and all(wg>=0. for aa,wg in G):
                        prob=loss[t]/(rr*gain)
                        if 0.<prob<1.:
                            root0=(mean/xr)*math.exp(-sigma*ndtri(prob)-half)
                            aa_min=min(aa for aa,wg in G);aa_max=max(aa for aa,wg in G)
                            lo=max(lo,root0-aa_max-1e-8);hi=min(hi,root0-aa_min+1e-8)
                            q=max(lo,min(hi,root0-sum(aa*wg for aa,wg in G)/gain))
                    tol=1e-10*cap
                    for _ in range(60):
                        d,dp=dsum2(G,q,xr)
                        d=-loss[t]+rr*d;dp=rr*dp
                        if d>0.:lo=q
                        else:hi=q
                        if hi-lo<=tol:break
                        qn=q-d/dp if dp<0. else -1.
                        if not lo<qn<hi:qn=(lo+hi)*.5
                        if abs(qn-q)<=.004:
                            qc=math.floor(q*100.)/100.
                            boundary=qc+.01 if d>0. else qc
                            db=-loss[t]+rr*dsum(G,boundary,xr)
                            if (db<=0. if d>0. else db>=0.):
                                q=qc+.005;break
                        if abs(qn-q)<=tol:
                            q=qn;break
                        q=qn
            q=math.floor(q*100.)/100.
            v=-q*loss[t]+rr*esum(G,q,xr) if q>=.01 else -1e100
            if not v>1e-12:return None,0.
            return (None,'participant',name,int(t),float(q),10**30,0.,0.),float(v)
        best=[];bestv=0.
        new,nv=best_fresh()
        if new is not None:best=[new];bestv=nv
        for o in old:
            if not (o[6]<=cash+1e-9 and o[7]<=yes+1e-9):continue
            v=val(o)
            # Retained equal-value orders win, preserving queue position.
            if v>0. and v>=bestv:best=[o];bestv=v
        if best:
            first=best[0]
            if first[3]<inside:
                new,nv=best_fresh(first)
                if new is not None and nv>1e-12:best=[first,new];bestv+=nv
            for o in old:
                if o[0]==first[0] and first[0] is not None:continue
                if o[3]==first[3] or not feasible([first,o]):continue
                v=val(first,o)+val(o,first)
                if v>bestv+1e-12:best=[first,o];bestv=v
        # Also retain a valuable existing two-level book even if neither
        # single order individually beat the best new first level.
        for i,o in enumerate(old):
            for other in old[i+1:]:
                if o[3]==other[3] or not feasible([o,other]):continue
                v=val(o,other)+val(other,o)
                if v>bestv+1e-12:best=[o,other];bestv=v
        for o in best:
            if o[0] is not None:kept.append(o)
            else:fresh.append(o)
        keepids={o[0] for o in kept}
        actions=[CancelOrder(o.order_id) for o in s.own_orders if o.order_id not in keepids]
        # All old nonkept reservations are released before new actions. Explicit
        # guard reconstructs actual kept reservation rather than virtual cover.
        cash=max(0.,s.cash-.01-sum(o[6] for o in kept));yes=max(0.,s.yes_inventory-sum(o[7] for o in kept))
        for o in fresh:
            t=o[3];q=o[4];req=_SC_SELL[t]
            cap=yes+cash/req
            q=math.floor(min(q,max(0.,cap))*100.)/100.
            if not math.isfinite(q) or q<.01:continue
            self.serial+=1;cid='oracle-'+str(self.serial)
            actions.append(PlaceOrder(_SELL,int(t),q,cid))
            covered=min(yes,q);yes-=covered;cash-=max(0.,q-covered)*req
            assert cash>=-1e-8
        return actions

class _MainView:
    # The five fields of a StepState the controller reads, with the protected probes' cash,
    # YES and orders taken out.
    def __init__(self,state,cash,yes,own):
        self.cash=cash;self.yes_inventory=yes;self.own_orders=own
        self.competitor_best_bid_ticks=state.competitor_best_bid_ticks
        self.competitor_best_ask_ticks=state.competitor_best_ask_ticks
def finalize_protected(cancel_probes,new_probes,controller_actions):
    # The controller returns its cancels first, then new asks.
    i=0
    while i<len(controller_actions) and type(controller_actions[i]) is CancelOrder:i+=1
    return cancel_probes+controller_actions[:i]+new_probes+controller_actions[i:]

"""Causal native-belief control overlay: no external/oracle context or helpers."""
def _plan_causal_fast(state,proposed,native,mem,named,grid100):
    cancelled=set();cancel_all=False;fresh=[]
    for ac in proposed:
        if isinstance(ac,CancelAll):cancel_all=True;fresh=[]
        elif isinstance(ac,CancelOrder):cancelled.add(ac.order_id)
        elif isinstance(ac,PlaceOrder) and (ac.side==_BUY or (ac.client_order_id is None and .009<=ac.quantity<=.011)):
            fresh.append(ac)
    records=[];eligible=set();main_own=[];resources={};removed=[];reservations=[]
    a=state.competitor_best_ask_ticks or 100
    pf=mem.prior_front;pq=mem.prior_prevq;BUY=_BUY
    for seq,o in enumerate(state.own_orders,1):
        oid=o.order_id;side=o.side;t=o.price_ticks;rq=o.remaining_quantity
        if side==BUY:rc=round(rq*t/100.,10);ry=0.
        else:
            _,_,ry=named[oid]
            rc=round((rq-ry)*(1-t/100.),10)
        reservations.append(rc)
        protected=(rq<=.011 and not oid.startswith('oracle-')) or oid.startswith('h') or side==BUY
        if protected:
            if not cancel_all and oid not in cancelled:
                resources[oid]=(rc,ry)
                if side==_SELL:records.append((oid,'sensor','SELL',t,rq,seq,rc,ry))
            else:removed.append(oid)
        else:
            records.append((oid,'participant','SELL',t,rq,seq,rc,ry))
            main_own.append(o)
            front=oid in pf or rq<pq.get(oid,rq)-.005
            if t<a or (t==a and front):eligible.add(oid)
    if abs(sum(reservations)-state.reserved_cash)>1e-6:raise ValueError('public reservation reconciliation')
    kept=[resources[oid] for oid in sorted(resources)]
    reservedcash=sum(o[0] for o in kept);reservedyes=sum(o[1] for o in kept)
    cash=max(0.,state.cash-.01-reservedcash);yes=max(0.,state.yes_inventory-reservedyes)
    output=[];sequence=len(state.own_orders)+1
    for ac in fresh:
        tick=int(ac.price_ticks);q=float(ac.quantity)
        if not 1<=tick<=99 or not math.isfinite(q) or q<.01:continue
        x=tick/100.;cap=cash/x if ac.side==_BUY else yes+cash/(1-x)
        if not (q==.01 and cap>=.01):q=math.floor(min(q,max(0.,cap))*100.)/100.
        if q<.01:continue
        ry=min(yes,q) if ac.side==_SELL else 0.
        rc=q*x if ac.side==_BUY else (q-ry)*(1-x)
        cash-=rc;yes-=ry;reservedcash+=rc;reservedyes+=ry
        output.append(ac if (q==ac.quantity and type(ac.quantity) is float and type(ac.price_ticks) is int) else PlaceOrder(ac.side,tick,q,ac.client_order_id))
        if ac.side==_SELL:records.append(('bridge-probe-'+str(sequence),'sensor','SELL',tick,q,sequence,0.,0.))
        sequence+=1
    own=[o for o in records if o[1]=='participant' and o[4]>=.01 and o[0] in eligible]
    sensors=[o for o in records if o[1]=='sensor']
    context=(own,sensors,(2*native.rate)*.5,native.meanretail,native.sigma,grid100,native.bel)
    mainstate=_MainView(state,max(0.,state.cash-reservedcash),max(0.,state.yes_inventory-reservedyes),tuple(main_own))
    return mainstate,context,[CancelOrder(oid) for oid in sorted(removed)],output


def _plan_spread3(state,proposed,native,mem,named,grid100):
    """Flatten native actions into the sell overlay's view without extra book scans."""
    cancel={};fresh=[]
    for ac in proposed:
        kind=type(ac)
        if kind is CancelOrder:cancel[ac.order_id]=ac
        elif kind is PlaceOrder:
            if ac.side is _BUY or (ac.client_order_id is None and .009<=ac.quantity<=.011):fresh.append(ac)
        else:return _plan_causal_fast(state,proposed,native,mem,named,grid100)
    sensors=[];own=[];old=[];resources=[];removed=[]
    a=state.competitor_best_ask_ticks or 100
    pf=mem.prior_front;pq=mem.prior_prevq
    allcash=0.
    seq=0
    for o in state.own_orders:
        seq+=1
        oid=o.order_id;side=o.side;t=o.price_ticks;q=o.remaining_quantity
        if side is _BUY:
            c=_SC_PENNY_BUY3[t] if q==.01 else round(q*t/100.,10)
            y=0.
            if oid in cancel:removed.append(oid)
            else:resources.append((oid,c,y))
        else:
            y=named[oid][2]
            if q==y:c=0.
            elif q==.01 and y==0.:c=_SC_PENNY_SELL[t]
            else:c=round((q-y)*_SC_SELL[t],10)
            if (q<=.011 and not oid.startswith('oracle-')) or oid.startswith('h'):
                if oid in cancel:removed.append(oid)
                else:
                    resources.append((oid,c,y))
                    sensors.append((oid,'sensor','SELL',t,q,seq,c,y))
            else:
                old.append(o)
                if q>=.01 and (t<a or (t==a and (oid in pf or q<pq.get(oid,q)-.005))):
                    own.append((oid,'participant','SELL',t,q,seq,c,y))
        allcash+=c
    if abs(allcash-state.reserved_cash)>1e-6:raise ValueError('public reservation reconciliation')
    resources.sort()
    reservedcash=sum(o[1] for o in resources);reservedyes=sum(o[2] for o in resources)
    cash=max(0.,state.cash-.01-reservedcash);yes=max(0.,state.yes_inventory-reservedyes)
    output=[]
    for ac in fresh:
        t=ac.price_ticks;q=ac.quantity
        if type(t) is not int or type(q) is not float:return _plan_causal_fast(state,proposed,native,mem,named,grid100)
        if not 1<=t<=99 or not math.isfinite(q) or q<.01:continue
        x=_SC_PRICE[t]
        if ac.side is _BUY:
            cap=cash/x
            if not (q==.01 and cap>=.01):q=math.floor(min(q,max(0.,cap))*100.)/100.
            if q<.01:continue
            y=0.;c=q*x
        else:
            cap=yes+cash/(1-x)
            if not (q==.01 and cap>=.01):q=math.floor(min(q,max(0.,cap))*100.)/100.
            if q<.01:continue
            y=min(yes,q);c=(q-y)*(1-x)
            sensors.append(('', 'sensor','SELL',t,q,seq+1,0.,0.))
        cash-=c;yes-=y;reservedcash+=c;reservedyes+=y
        output.append(ac if q==ac.quantity else PlaceOrder(ac.side,t,q,ac.client_order_id))
        seq+=1
    context=(own,sensors,(2*native.rate)*.5,native.meanretail,native.sigma,grid100,native.bel)
    mainstate=_MainView(state,max(0.,state.cash-reservedcash),max(0.,state.yes_inventory-reservedyes),tuple(old))
    return mainstate,context,[cancel[oid] for oid in sorted(removed)],output

# ================================== Data tables ==================================
# The DP table of inventory targets (yes_stock): 2,001 steps per queried cost.
# Initial fair exceeds 0.08: Poisson(lambda*2000 <= 6) has at least 0.60
# mass at N<=6, each such Gaussian component has probability >0.13.
# Every later competitor ask comes from a bid >=1 moved up by the spread.
# Thus asks are >=spread+1, and the lookup reaches only the intervals
# containing 1/k (2<=k<=99) or 2/k (3<=k<=99). Empty columns are unreachable.
DP_COSTS=[0.005, 0.01, 0.02, 0.04, 0.06, 0.08, 0.1, 0.12000000000000001, 0.13999999999999999, 0.16, 0.18, 0.19999999999999998, 0.22, 0.24, 0.26, 0.28, 0.30000000000000004, 0.32, 0.34, 0.36000000000000004, 0.38, 0.4, 0.42000000000000004, 0.44, 0.46, 0.48000000000000004, 0.5, 0.52, 0.54, 0.56, 0.5800000000000001, 0.6, 0.62, 0.64, 0.66, 0.68, 0.7000000000000001, 0.7200000000000001, 0.74, 0.76, 0.78, 0.8, 0.8200000000000001, 0.8400000000000001, 0.86, 0.88, 0.9, 0.92, 0.9400000000000001, 0.9600000000000001, 0.98, 0.99, 0.995]
_DP_CODE={'0': 3, '10': 4, '110': 2, '11100': 0, '11101': 5, '111100': 1, '111101': 6, '1111100': 7, '1111101': 8, '11111100': 9, '111111010': 10, '111111011': 11, '1111111000': 12, '1111111001': 13, '1111111010': 14, '11111110110': 15, '11111110111': 16, '11111111000': 18, '11111111001': 21, '111111110100': 17, '111111110101': 19, '111111110110': 20, '111111110111': 23, '1111111110000': 22, '1111111110001': 26, '1111111110010': 27, '1111111110011': 29, '1111111110100': 33, '1111111110101': 42, '1111111110110': 45, '1111111110111': 53, '1111111111000': 58, '1111111111001': 67, '1111111111010': 75, '1111111111011': 111, '1111111111100': 167, '11111111111010': 24, '11111111111011': 25, '11111111111100': 31, '11111111111101': 34, '11111111111110': 35, '11111111111111': 37}
_DP_COLUMNS=(
    "",
    "1e7fefff1ff5fff7fe7ff1ff7ff87faffaffa7f7feffdbfafebf9fe7f9fe3f7fc7eff7fbfd7ebf5fafd7e7e7e7e7e7e7e7dfcfbf7efdfbf7efcfbf3efcf9f3efcf9f3e7bf3e7cf7e7cf7e7bf3df7e7befbefbf3df7df7befbefbef7df7defbef7defbdf7bef7def7defbdef7def7bef7bdef7def7bdef7bdef7bdef7bdef6ef7bdef6ef7bdbbdeddeddef6ef6ef6eddeddbbdbb76eddbb76eddbb76ed76eddaeddaed76bb5daed76bb576aed5dabb576abb55daabb5576aabb555daaaabb555555daaaaaaaaaaaed55555555554aaaaaaaaaaaa955555552aaaaa55554aaaa95554aaa95552aa9554aaa5552aa5552aa554aa954aa954aa954aa954aa552a954aa552a952a954a954aa54aa54aa54a954a952a952a552a54a952a952a54a952a54a952a54a952a54a952a54a952a54a952a54a54a952a54a94a952a54a54a952a52a54a952952a54a94a952a52a54a94a952a52a54a94a952a52a54a54a952952a54a54a952952a54a54a952952a54a54a952952a54a54a952a52a54a94a952a54a54a952a52a54a952952a54a952952a54a952952a54a952a54a54a952a54a952a54a54a952a54a952a54a952a54a952a54a952a54a952a54a952a54a952a54a952a952a54a952a54a954a952a54a954a952a54a954a952a552a54a954a952a552a54aa54a954a952a952a552a54aa54aa54a954a954a954a954a954a954a954a954a954a954a954aa54aa552a552a552a952a954aa54aa552a952a954aa54aa552a954aa54aa552a954aa552a954aa552a954aa552a954a",
    "3cffcffdbfe9ff2ff7ff3fe3fc7f7fedfd7f3fcff1fc7f1fbfd7eff5fafcfd7e7e7e7dfcfbf7efdfbf7e7df9f3e7df9efcf9f3e7bf3df9efbf3df7df7df7df7df7defbef7df7bef7defbdefbdefbdef7def7bdef7bdef7bdef7bdef7bdbbdef6ef7b77b77b76ef6eddeddbb76eddaeddbb5daed76bb5daed5dabb576aaed576aabb555576aaaaaaaabb555554aaaaaaaaaa555552aaa95552aa9554aa9552a9552a954a954a954a952a952a54a94a952a52a54a54a54a52a5295294a94a5295294a5294a5294a5294a5294a5294a525294a529294a529294a52529494a52529494a52529494a4a52929494a4a52529294a4a5252929494a4a525292949494a4a5252929494a4a5252929494a4a5252929494a4a5252929494a4a5252929494a5252929494a52529294a4a52529494a4a529294a4a529294a4a529294a52529494a529294a525294a4a5294a4a5294a4a5294a525294a529294a5294a525294a5294a5294a4a5294a5294a5294a5294a5294a5294a5294a5294a5294a5294a54a5294a5294a5295294a5294a54a5294a52a5294a54a5294a54a5295294a52a5294a94a54a5295294a94a52a5295294a94a54a54a52a5295295295294a94a94a94a54a54a54a54a54a54a54a54a54a94a94a94a952952952a52a52a54a54a94a952952a52a54a94a952a52a54a94a952a54a54a952a52a54a952a54a54a952a54",
    "79fff7fd3ffbff3fe3fbff6febf9fe3f7fbfdfebf5f9f9f9f9f7efdfbf7e7cfbf3e7cf7e7cf7e7befcf7df7df7befbef7df7bdf7bef7bef7bdef7bdef7bdef7bdef6ef7b77b77b76ef6eddaeddbb5daed76bb576abb5576aaabb5555555555554aaaa95552aa954aa552a952a54a95295294a94a5294a529494a5252929294949492929292524a49492524949252492924929249252492494924924925249249249249252492492492492492492492492492492492492492492492492492492492924924924924924924924924924924924924925249249249249249252492492492494924924924a492492494924924a492492524924949249292492524925249252492524925249292494924a4929249492524949252494925249492924a49492524a49492524a4949292524a494949292524a4a4949492929252524a4a4a494949494929292929292929292929292929292929292929494949494a4a4a4a52525292949494a4a525252929494a4a52529294a4a52529494a4a529294a4a529294a52529494a529494a529294a529494a5294a525294a5294a525294a5294a5294a5294a5294a5294a5294a5294a5294a5294a94a5294a5294a94a5294a52a5294a52a5294a54a5294a94a52a",
    "3cff3ff2ff3fe9fdffafe7f8fdfebf5fafcfcfcfbf7efdf9f3efcf7e7cf7e7befbefbefbefbdf7bef7def7def7bdef7bdef7bdeddef6ef6eddbb76eddaed76bb576abb555daaaaaaaaaaa95554aa9552a552a54a54a529525294a4a5252524a49492924949249252492492492491249249224924892489248924492249124492248922489224892248922489224491244892248922449124489224891244912448922489224892244912449124892248922489244912449224912449224912489244922492249124912491249124912492249244924912492449249224924922492492489249249248924924924924924924492492492492492492492494924924924924924929249249249292492492524924949249252492494924949249492494924a49252492924949252494924a492925249492524a49292524a49292524a4949292524a4a494929292524a4a494949492929292525252524a4a4a4a4a4a4a4a4a4a4a4a4a4a4a5252525252929292949494a4a4a525252929494a4a4a52529294a4a52529294a4a52529494a52529494a525294a4a529494a529494a5294a4a5294a525294a5294a529294a5294a52",
    "1e7f1feffe3fbff5fc7f1fbfd7e7e7e7dfcf9f7e7df9f3df9efcf7df7df7defbdf7bef7bef7bdef7bdef6ef7b77b76eddbb76bb5dabb55daaaaabb4aaaaaa5552a954a94a94a5294a525252524a492524924924924912492249124892244912248912244889122444889122244448891122224444888911122224444488891111222244444888911122224444888911122244448891122224448891122444889112244489112244889122448891224489122448912248912244892244892244912248922449124491244912449124492248924492249124892449224922492249224924492489249124924492492249249244924924922492492492492249249249249249249249249249249249249292492492492492924924924a4924924a49249292492524924a4924a4924a4925249292494924a49292494925249492524a492924a49492924a4949292524a494929292524a4949492929252524a4a4a4949494929292929292929292929292929292929292929494949494a4a4a5252529292949494a4a5252929494a4a52529294a4a52529494a4a529294a52529494",
    "79effb7fa7f5fc7f1fafd7e7e7e7dfbf3e7cf9f3e7befbefbefbef7defbdef7bef7b77bdef6ef6eddbb76bb5dabb555daaaaaaa5554aa552a52a529494a4a492924924924924922491244891224489112224444488888888891110888888888884444442222211111088884444422221111088888444422222211111088888884444444422222222222222222222222222222222222222444444448888891111222224444888911222244488911222444891122244891122448891224489122448912248912244912244912248922449124491244912449124492248924492249124892449244924492449248924912492249249124924892492491249249249124924924924924924892492492492492492924924924924924924a4924924924a4924924a492492524924a492494924929249492494924a49252492924a4929249492524a492924a49492524a49492524a4949292524a4a494929292524a4a4949492929292525252524a4a4a4a4a4a4a4a4a4a4a4a4a4a4a4a4a5252525252929292949494a4a525252929494a4a4a52",
    "79cff6fedfcfeff7f9fafbf7efdf9f3e7cf7e7befbef7defbdf7bdef7bdef7b77b77b76ed76bb576aaaaed4aaaa954aa54a5294a4a49492492492491248912244891112222222222222111108884442211088422110844210884221084421088421088421108422108442108842210884221088442110884421108844422110884442221110888444222211111088888844444444222222222222222222222222444444444888889111122222444488891122244448891222444891122444891224448912244891224489224489124489224491224892248912449124892248924491248924492249124912489248924912491249244924912492449249244924924922492492492491249249249249249249249249249249249249252492492492494924924925249249252492494924929249252492524929249292494924a492924a4925249492524a492924a49492524a4949292524a4949292524a49494929292524a4a4a4949492929292925252525252525252525252525252525252529292929494",
    "3ce7f6febf7fbfd7e7dfcf9f7e7cf7e7befbefbef7def7def7bdeddef6ef6ed76ed5daabb55552aaa552a5294a4a4a4924924922489122444888888888844422110884211084210842104210841084108410821082104208420841082108210421042108210821084208421084210842108421084210842210842210884211088422110844221108884422111088844422221111088888844444444222222222222222222222224444444488888891111222244448889112224448891122244889122244891222448912244891224491224489224491224891244912448922491244912449224912449224912491248924892489249124922492449249224924912492492249249249244924924924924924924924924924924924924924924924924949249249249492492494924924a4924929249252492524925249252492924949252492924a492924a492924a49292524949292524a4949292524a4949292524a4a4949492929252524a4a4a494949494949292929292929292",
    "79cfdff1fbfd7ebefdfbf3e7cf9efbefbef7def7def7bdef6ef6eddbb5dabb555555552a952a5292925249244922448911111111108844210884108420841082104104104104104104082082041041020820410410408208208208208208208208208410410820841082104208420842084210421084210821088421084210884210884221088422110884422110884442211108888444222221111110888888888844444444444448888888888911111122222444488891112222444889112224488912224488912244891222448922448912244912244912248912449124491244912449124492248924492249124912489248924892489249124924492491249248924924912492492491249249249249249249124924924924924a49249249249249249252492492492924924925249249492492524924a4924a4924a4925249252494924a492524949252494925249492924a49492924a4949292524a494929252524a4949492929252524a4a4a4949494",
    "79cfbfbfd7ebefdfbf3e7cf7e7bef7df7bdefbdeddef6eddbb76aed5555554aa54a5294924a449244889111111088442108420841041041041020810204081020404081010204040808102020408081020408102040810204102041020810408208204104104104104108208210410821042084208420842108210842108421084221084211084421108442110884422110888442221110888444422222111111108888888888888888888888888911111112222224444488891112224444889112244488912224488912244891122448912248912244892244912248912449122489224892249124491248924492249124892489244924492489249124922492489249244924924892492492489249249249249248924924924924924924924a4924924924924924a4924924925249249252492492924924a4924a492494924a4924a492524929249492524949252494925249492524a49492524a4949292524a4949292524a4a494929292",
    "f39dfdfe7e7dfbf3e7cf7df7df7bef7bdef7bdbbdbb76aed555d55552a5294a4924924891122221110842108210410208102040808080808080402020100804020100804020100804040202010101008080808101010202040808102040810208102081041020820810410410410820821041082104208420842104210842084210844210842108842110844211088422110884442211108884442221111108888844444444422222222222222224444444444888889111122222444488911122244488911224448891224448912244891224489122448912448912448922449124491224892249124491248922491248924492249224912492249224924492489249224924922492492249249249224924924924924922492492492492492492492524924924924924925249249249292492492924924a4924929249252492524925249292494924a49252494924a492924a494925249492924a4949292524949292524a4a",
    "79cf3f8fcfbf7e7cf7e7bef7defbdef7bdeddbb76bb55daa9554a94a4a4924892222422210882104104082020402020100802008010020040080080100100200200400801002008020040200802010080402010100808080808081010202040810204081020810408204104102082082104104108208410821042104210421084210821084421084210884211084421108842211088444221110884444222111110888888444444444422222222222444444444448888889111122222444488911122244488911224448911224448912244891224489122448912448912448922449124491224892249124491248922491248924492249224922492249224924492489249244924922492492249249249224924924924924924492492492492492492492924924924924924925249249249292492492924924a4924929249252492524925249292494924a49252494924a492924a492925249492924a4949292",
    "3ce73f5f7efcf9f3df7df7bef7bdef7b77b5daed555554a94a5249248911221108421041020404040200801001000800200080008000800080008001000100040010004002002002002004008010040100401008040201010100808080810102020408102040810408104082082041041041041082082104208410821042108210842108410884210842108842110844211088422110884422211108844442221111108888884444444444422222222244444444444488888911111222244448889112224448891122244889122244891122448912244891224891224491224891244892248922449124892248922491248922491249124892489248924892491249224924492492249249224924924892492492492249249249249249249249249249249249249249249492492492492524924924a49249252492494924929249252492524925249292494924a49292494925249492524949292",
    "1e739f3efdf9efcf7defbdef7bdef6eddabb55554aa529249248911108421041020404010020020004000080000200000100000020000010000010000080000800040002000400100080040040040080100401004010080402010101008081010102020408102040810208104082082041041041042082084104208410841084108421042108421084211084210884221088422110844221108884422211108888444422222111111110888888888888888888889111111122222244444888911122224448891122244488912224489112244891224489122448912248912248912248922449124491244912449124892249124892449224912491249124912492249244924892492449249224924924892492492491249249249249249249249249249249249249249249249292492492492524924924a492492524924a49249292492924929249292494924a49252494924a49292",
    "3ce73bf7e7cf7df7bef7bdef7b76ed5daaaa54a9292491222221082081010080100080000800000002000000000000000001000000000000080000000400000200000800020001000200080040020020040080100401008020100808040404040404080810202040810408104082041041020820820841041082084108210821082108420842108421084221084211084421108442210884422211088444222111108888444442222222221111111111111111112222222224444448888911112222444888911222444891122244889122448891224489122448912448912248912448922449124491244912449124492248924492249124892489244924492489248924922492449249224924922492492491249249249249124924924924924924924924924924924924a492492492492524924924949249249492492524924a4924949249492494924a49252492924",
    "79ce3efbf3defbdf7bdeddbb76aaaaaa529492448888420820404008001000000002000000300000000000000000000300000000000000008000000000002000000200000400008000800080020010010010020040100401004020101008080404040808101020408102040820408208104104104104104104208210420841084108410842108421084210842108442108842210884422108844422110888444222211108888844444442222222222222222222222222444444448888911112222444488891122244488911224448911224488912244891224489122449122449122489124489224892248922489224892449124892449224912491248924892491249124924492491249248924924892492492249249249249244924924924924924924924924924924924a4924924924924a49249249292492494924924a49249492492924929249292494",
    "1e739cfbf3df7bef7bdeddbb5daaa954a4924891088208101001000000800000c000000001800000180000030000000c000000000c000000000000000000000001000000002000002000020001000200040020020020040080100402008040201010100808081010202040810204082040820410410208208208210410420841082104208421042108421084210842108422108442110844221088442211108844422211108888444422222211111111111111111111111111111122222224444488889111222244488911122444889112244889122444891224489122448912448912248912448922489124491244912449124892249124892249224912489248924892491249124924492491249248924924912492492489249249249249248924924924924924924924924a49249249249249252492492492924924929249249492492924925249252",
    "1e739cf7e77def7bdef6ed76aaa54a492488884102020020000000000000c000030000c000300018000600018000180000c000006000000060000000000000000000000000008000000200000400010000800100080040040040080200402008040201010080808080810102020408102081020810408208204104104208208410420841082108210821084210842108421084221084221088422110844221108884422211108884444222211111110888888888888888888888888888911111112222244444888911122244488911222444891122444891222448912244891224489224489224489224491244892248922489224892449124892449124912489244924492449248924892492249248924924492492449249249224924924924924892492492492492492492492492492524924924924924a49249249252492492524924949249252",
    "",
    "79ce71ef7bdeddbb55552a49224220810010000000c0003000c00600c0180300600c0300600c018030030060060030018006000c000c0006000060000060000000060000000000000000004000000008000008000100008001000400200200400801004010080402010080808080808101020404081040810208104102082082082082084104208210420842084210421084210842108421084421088421108842211088442211088844422211108888444442222222211111111111111111111122222222244444488889111222244488891122244489112224488912244889122448912244891244891224891244892244912449124491244912449224892449224912489244924492449244924492489249224924912492489249249224924924922492492492492492492492492492492492492492492492492524924924924a4924924",
    "3ce739cf9def6ed76aa54944910820400009800001800c00c0300c0300c06030180c060180c060180c0300c03006018030030060030018006000c000c000300000c00000060000000000000000000000000080000008000040001000100020010008010010040080201004020101008080808081010202040810204102041040820810410410410820821042082108210421082108421084210842108421108422108842211084422110888442221110888444422221111111088888888888888888888888888889111111222224444488891112224444889112244488912224489112244891224489122448912248912248912448922489124491244912449224892449124892449224912491249124912492249244924892492449249224924924492492492449249249249249249249124924924925249249249249249249249492492492",
    "",
    "",
    "",
    "3ce739ce77b4a9244420000000c030303060c1860c3061861860c30c30c3061861860c30c1860c306183060c3060c0c1830606060c0c0c0c0606030180c060180600c018030030018006000c00060000c0000060000000003000000000002000000000100000100001000080010004002002004008010040200804020101008080810101020204081020410204102082041041041041041082084104208420842084210421084210842108422108422108442110884221108844422111088444422211110888888444444444222222222222222244444444448888891111222224444889111222444889112244489112244889122448912244891224489124489124489224491244892248922489224912449224892449224912489248924892489248924912492449249124924892492491249249248924924924924924492492492492492492",
    "f39ce73954a488810000180303060c1860c30c30c318618618c30c318618630c30c30c30c30c30c30c1861830c1860c1860c18306060c181818181818180c06030180c0300c01803003003001800300030000c00006000000180000000000000000000000800000020000080002000200080020020020040080200802010080402020202020202040408102040810408104082082041041041042082084108208420841084208421084210842108421084421088422108842211088442211108844422211110888844444422222222221111111111111122222222224444444888891112222444488911222244889112244489112244889122448912244891224891224491224891244892248922489224892248924491248922491249124892449244924492489249124922492491249248924924912492492489249249249249244924924924",
    "",
    "",
    "",
    "",
    "",
    "",
    "",
    "79ce739ce7386318cc663333319999999999999999999999b333333333333333199999999998cccccc666633331998ccc66333198cc633198c663198c63318c6318c6318c63186318c318630c618c30c31861861860c30c1860c3060c183060c18181830301818180c060180c0300600c01801800c0030003000180000c000001800000000000000000000000004000000800004000100010004002002002004008020080201008040202020202020204040810204081020810208204104104104104104108208410821042084210421084108421084211084210884211084421108442211088442221108884442222111108888884444444442222222222222222244444444448888891111222224444889111222444889112224488912224489122244891224489122449122448922448922449124489224892248922489244912489224912489244922492249224922492249244924",
    "1e739ce739ce726633333333333333333666666ccccd99999b33333366666666ccccccccccccccccccccccccc6666666333319998ccc66633198cc6633198c663318c66318c6318c6318c6318c618c618c318630c61861861861861861830c1860c183060c183030306060603030180c0601806018030060060030018003000180001800000c00000000000c00000080000000000080000040000400010002000800400400400802004020080402010080808080808101020204081020410208104082081041041041082082104108210420842084210821084210842108421108421108442110844221088442221108844422211110888844442222222111111111111111111111111111222222224444448888911122224448889112224448911222448891224448912244891224489122449122448922449122489224491244912449124492248922491248924492249124912491249124912",
    "",
    "",
    "",
    "",
    "",
    "",
    "",
    "",
    "",
    "",
    "",
    "",
    "",
    "",
    "",
    "",
    "",
)

def _load_tables():
    """numpy and the native player's tables: the normal cdf on a grid (the core's and
    flow_learning's retail tails) and the recalibration net's weights."""
    global np,_TAIL_Z,_TAIL_CDF,_CAL_Q,_CAL_MU,_CAL_SD,_CAL_A,_CAL_AB,_CAL_B,_CAL_BB,_CAL_C,_CAL_CB,_CAL_ARAW,_CAL_ABRAW,_CAL_RAWLO,_CAL_RAWHI
    import numpy as np
    _TAIL_Z=np.linspace(-9.,9.,36865)
    _TAIL_CDF=np.array([.5*math.erfc(-z*_SQRTH) for z in _TAIL_Z.tolist()])  # v3's stored table, rebuilt from its formula
    _CAL_Q=np.array([.1,.5,.9])
    _CAL_MU=np.frombuffer(bytes.fromhex("5902ff3ea5941b3f2daf5e3f8cc46a3e1a465c3a133497be1bb708bd4b426a3ef16135be5daa353e1d9f1f3fface1e389185afb8a0cad0b9f22cc538381db2395571813f72527e3f9d267e3f54057e3f0169073d7a1fba3ceb860a3f007be9354f2c0336"),dtype="<f4").astype(float).reshape((25,))
    _CAL_SD=np.frombuffer(bytes.fromhex("b8e75f3e8e44833ebea5a23d8419de3d05beec3d0a87073ecdcc4c3d6e00083ef1e3fb3dc587fc3da2c48d3ead8d443e9c99443e9aa6263fc56d1e3fefeaf03ee383443edab13e3ecdcc4c3dcdcc4c3d3795f03d5cb9c73db3e8ab3dcdcc4c3dcdcc4c3d"),dtype="<f4").astype(float).reshape((25,))
    _CAL_A=np.frombuffer(bytes.fromhex("102b533e13759cbdb4f0493e9fdf21be26dfee3eee94f2be89352f3f68e1963e15d4943de745963d02b457be0482833ea14f6b3db73920bb886f093e0c3ca5bdc13f58bcfd391b3a254097bd1eabb4bd0b1bb2beb6c1843dc3748fbdcdae9b3ea174293fb9884dbf8319273e579d4b3ec2f57abd0e0689be36db66bd9c7af0be40535d3e8b56903d340e333f18dc583d6bd701bfe009143fda319a3d16ba86bd3ddf263d3ab36439a0c4b13b5fe73f3d1f20353d88f83b3e4ebe15be085021bd98c634bf8d6f13bf43a3853e84ff7c3c1dc334bed9acf4becda2853d69d68d3efd9228c0570a2f3eacf2e3bd42de293e7815c43ddbdaa23e4f8b033ffc6935bfd4e3393c06d6b4bd89ab6ebdfe680ebdf2e6913e8e344f3e54409ebe2051293fad5f683d9e1c04be2552b0bf0a1d93bf83fc493db5eba83e74bd06bffb38b5bdc6c1e7bde5fd603ed4c9033f64e52ebef163403eda309abe41a929bed9f341bee94632bc43f8493ec8f536bd11e765bd82d436bd3e4b4bbf569695bf4ba83b3e897e29bd03c307be42bb63bd2eddd1be4cf2d6bd2724aabeb10dd83e26f83fbf25b885bd91d2f3be0bedc13f9aea533e11770b3fa2f7953fece2123ff9d22c3f598a7abf1289adbe283c903ddd020b3dc83431bd80a6303d9084723e2d29e13e54afcfbe32d6453dd95299bd319c033fddfd27bf3ec02b3df70c443ef8085ebefbd96e3deeaf243d62f35abef28e8cbfcfbd163efc599ebd76c215befbdb24be4d94853f6d274dbf8db745bf8f0b58bde7564a3c80efdcbc487e80bd9d4e1bbd859bc8bd6b34043fecbd123e57dc6abc0f901e3faa7f253f36f213be3577b3bd70987dbe7b4a723f8546d2bd1b6e653f80e7b53f344b89bf7a2cd03ea5d22140be86a43e982647beb522bfbe915ed93d8a77be3c760b713c9130023c81e9823c74efa03ecedf973e9fded9bdebbe003df85431bd3d97fcbe168f36bf2d2d3c3e6fa085be6676723fa88b2ebf0016abbe0ef140bfd336fb3b3c714e3fe51731bd2bdaa1be987d843de4a799beeada2e3fe316ddbe3e5a2a3d41d2613c204717bde99d80bd985774bfbe3545bf24a7e2bc70f5153dcdcc083ee926be3ed9991abf1b41483dfa69a33d98bc26bed534303efb56cebd78a173be37fe493fffdfc8bb2e2b103f199dd23ed457c33dcb1c1bbef7ceb6beb4097c3f79018fbee66a43be2de80a3d455dbabb2e1716bfb88a01bfb6cf463ca87007bf7bf9da3d6a21babe90595bbfa6bfbabc718f043db2b3e4be2c22d43e2cc63ebed39b5b3e784e294021c125bf5849823f16c47f3e44090abfc16ee2beeab1593ef9757abf37e6143e3debe9bdf68d68bcaee858bcd1daacbe50ecc9bebe9423bec47a8ebe6a12413ca2a65b3e9299493f6094603ee3162e3dc5cb683e06ae4cbe8916593c6190edbe3277523fa981003f6eb51b3d3404853ec1ab053e8038ed3e7a9134bfb5fe0b3f9e59d03d2b759cbdb9e3bf3b2b221e3c9dbeaabd05a6f43cdb76993ee6ba9ebcd43ec03ddf1f323febb3603f1e149dbf30e42f3e2f22be3cefc60b3ee7937ebef4dc8dbd08bbdbbfb6441c3dff44b0bee43640bf5b7369bd53c88c3d6fbbe93e710148bd53e855bc1195e83df0fa69bdd78d0cbddc10cf3d113d1ebce83d52bd4e204e3eff1d873ca376703e5095d23eaf48053f42b5e9bd53fd5b3e0a4592be5321593d5c2f55bd2883b5beb62d723e3facd2bd64db8dbe3e9857be745c03bfb9c3e33ed9cccc3d9efd2b3dabe699bd891ad73c5ac69dbd6aef51bfb826c4becd12073e151006be4cff06bb7b8e413fbdf7673f14d843be12cc5a3c4f9683bd2ceeaebefee2c5bd11dfc33ecaf5d43d45e21dbeb94c9a3c65a5a6bd753f883e1fb5723eaabf0abfff96d93e66a5013d3bc3603da229abbd51afde3d998e8c3e740b443e79f50ebf6a71973fae16803d9f4fb2bfa404873e5391253dbd3c7c3ed89489bebcc72cbe42ad843ebe2688be7882104055eeccbea9a3173e8c74c9bdcce9633dcf9cae3c9583333eeb69ee3e5f89d83ce64ce3bbf5dcecbcabc70cbd04d68dbde07ceabd0da93dbe24f871bf9e56c4bc274cd73eda1b05bc5289dd3d8da5e3bdf079b5bce7fa93bd3376083eadd4103f37c4c0bf6f11eb3c7a25893f9fda563cd4d0e2bc165675be3f5348bf9bae5c3e9e6a03bf091d193e8dc10dbd127d9dbc576051bdf5e079be3950133e046ad9be3844533ecbfd443f0441c4beaba6293efcd1363d90baae3e57780ebe0af5423dc8f638be0052a5bd8b3a96bc7a17fdbeddfaa7bed864123e8c9e82bee232b7bdb5f1623e79a05bbe7d70793cd044143b66b0f039fec2b73e711e0f3fe43e073ed770ca3f34efb83d77ffd6bc3b12b43d23d2d3bd75fa8f3e5edc5e3e7ce50abfefc5f3bd9de736bec6d4433ee33d133e4c3c40bedbf975bd513d723efec689bce8547fbeaeea43beba8d2cbe974401bf70a395bd5792933e4146aebe418c8ebdd2699c3d01d1b2bf6a21dabc9bbe823f5d6ed2befdf289be254a273e07c13fbec0af323e8d7d28bc7e0bd6bd419a893fce64a8bd6af5a53ee464e0bef3295cbffa17eb3fa5154a3f462f9b3e43b89dbb6514293d28a862bcb6dd353dcd2b35bd87b0913e5e278c3db30509bf28f42ebd1213873fc5d9babe739485bf41736c3d616c03bf7256193e37fcc8be9f29f03e55f9663f0f229cbef78f333fcec4b33e1353a13db245543e636481be0562bebd9cbb2dbef79c27bdc3be1b3de46689bd46a1093fec43a13efefd4b3dcc896dbddf9e0e3e3cd2373f244023bfa529443df58be13cf5445b3eb2bd523d617eea3bbab5833e5ebd9f3e717addbe34f370bcb6d444be297fdb3d2057863e5a9108be1b02613fdb07893e1ba0b13e1e7c13bb19fe5abcc6040abe2de289bdbad458be0ae5413e810e5abb993fbcbe64840d3fd72ff43cd1658f3d54e9e3bd05d80a3fcc2b89bdb35ece3d563992bffcea22be5a1ae4bea61631bf2c8c26bde8898f3ea8881bbe7492c4becfb6e7bbd066673db4ee863c2bab15bd7d7a463d71e2d0be365409be4f6f193e0284c73cf36fb4be88fd1ebe54d68f3d09788c3d86e61f3efaf607be5cd1903d120a1abfa4d046401270153ee67dacbe2ee7d2be7bfc16bd8cc6363f94a3bebe99c01cbfd3eca73d932b453dd3d5d53aad100abd0d3926be3ef2c4be287992bd5cd3403ee67547bd44bfdcbe60eeb8bef09aad3dcc81983e09efa6bc1fa9bc3e08c7a73debdaaebb921d87bc888e8d3eca6b56be5cc721be104235be362970bfcbbd923e615ed33e689633bd382abf3d6b91313c46bfad3c5e19503f66c59d3f8ed0c6bee6c9113f64d8abbd4a334c3f87c7023f0ca882bedd4ebb3dbd3bc43e4869c9bec4ec093e135a60bff4d5debe6fc5523f829284bd56e20ebfc70748be66f9113f5d130dbfc7c3a13e1fd2323d3b991f3e2ac1483da8f30f3d2691303e7361d33db621a0bd09fc4fbf95d3453d2df2023f666fd5be3ddf34bd559fdcbc1790723d1931eb3df48487beb99dfc3e0c41aebea1925dbecfae983e75eb083ebeead43d15ef443e4d99203e2d9286be7048093e63d032be47db1dbd0c8c61bdc4da003fd9ac6f3e7041bd3f11593abd3e7c923c9f201d3ffaf345bf14f261bd24bd9ebcb83046bd4c3b82be2cd5a43efe222e3e5d9095bda3eeec3be4e6debe307ded3c4096dd3d8c9283bf476407bfd982133e679ccf3d5fec0dbe1c2ce5bc84afdd3c7600a7be40f441be0ed4713d64301abed5ad973d5fe5073f89b8683d6307b83efa7a10bdac92ddbe801d923e1c03323e99fd1abeb189fcbe617f4bbea62fefbe20b5eebef37efe3d76a8c73e0de8363e62cd843e37e4273f1b9fcdbde61a0dbdb502bd3dce01203e801c03bea058bd3d8440d8bda0899fbe5678dd3e6e15203e1247113c9c221f3e21e35fbd638c1fbf846d4dbd2c2d5fbf2d18073facd1043f0a531c401cd0ae3ed26c49beb69bbb3aead2d4be89c70d3f3acff4bc650a923c4f145dbb61300dbdc8ee06bf7eed38bf85d8693d15f0b93d2efdc43d508f873e22304a3fca9fbd3c257decbdb3a5fabeb323f6bef66613bc893ec83e3cd500be6643a8be69357dbd4cdc48bfa263e1bdf62f893e77ae013e8017433f3cd8553c27ba793e622f00beeb8ee9bb995bc4be088f7cbe1732853f351671bf1b3814be98dabbbe392b3abf0f8c01be362c143d490dcd3d3efd343f6023cabdef644b3ed36bc93f4ddd25be865073be58e7c13de2004cbe7a631cbf5f8c243fb91d8cbefdf1713d00cd93bd6d286cbde039f43b21d9813fc1c9aa3ff4eae23d67b75eba9643733df87c843fd6928c3fa50ca83d0a121b3ec35219bc49bc433e1fc49c3d8548b23df8f38e3e824dcc3d3610aabe4122b1bb9c3ed1bcc66c283f306eed3ff54cbd3fb0e657bd9b56b9bd99ea63bda58c63bd225b73be9a45d6be7a73053e49e41c3f65d7983d3503c93efa0b2e3f"),dtype="<f4").astype(float).reshape((32, 25))
    _CAL_AB=np.frombuffer(bytes.fromhex("01de663e879f1e3e58a6babe88214cbecda5533f3cc98f3dc221923fdc5173bedc879f3ec83909bf24622b3f563808bdf635ccbe1a581b3f03a102bf2e564bbf0754f63d03fa98be7520b1be38001cbe2851bdbe84b2da3ed17e8c3e102b43bd773a423e466e4d3f43c0b1be16ef853d63ef64bf813b393c9dcf9bbe722ca0be"),dtype="<f4").astype(float).reshape((32,))
    _CAL_B=np.frombuffer(bytes.fromhex("a8ada53da49ca5bc63d505bd1e5f2cbedccfd9bd2a93143f3a102abf73229cbe5f9baabea35de8bc5d74da3dc1a9edbe7c0c97be71a61dbe20d960bda308423ef4d2a13b0e4fe83c8177623e3bcee63e012fcbbda80ad53e50e500bdf3b526bebbcc9dbe82bfabbe174cb6be8657bb3c2f593dbebd28863d25d9913e82bebabebd77763e549cd13d94fb23bf47adc03d0fd3f2bd10080c3e0173693d79d75b3e45e0a4be8eed183fe1cfb7bd48ae74bed0f24ebd26c8633e893c06bfe6d2ccbef38491be2ff606bd63daf1bdad530b3e12e9043ef375ad3e61fb183fa2441bbf2b4d593e89a2e33ead37803ed91cfb3d2ba1febd7561883eb7166abf8f52b73dbf87d33ea563883ef96418bd77601ebda880363fd8aac7be0a6fa8be1ff2233eb8ee7ebe6f7edd3d2d3322bd2a118abea20b183fca94813c0edbd43d1125f93e996e01be62b4d13de4e9a43eeb69a23e07022d3eb90c0c3f8422d2be53cce0bece0d913eb779d8bd1629bfbe2acfda3ed7ee7d3d04d1b4bedd56af3e9add22bea809c3be209e54be3c9eceb94e969f3ea476c1be9d761bbeb6ff16bed0438a3e9097873edfa6edbbb0beb23dde86ff3ee02512be8fd41ebfc95fbdbe2a2ff23e398f1bbf156e813ea2ec393ed27727be59f6023ed523753e7725adbe41459ebe0248f03eabfba8becdb1b23db9f85d3e67fe823e0c019dbe92beb5be11a9c5be2bd7a0bea7d2643e9623543fdcd29c3ea3098abeb5d802bf758cab3e1ec0303fa1dad4bd6e4308bf691639bf82da9fbe0b42ce3e66c909bf0bebeb3b411956bd3e5e3dbe7dcf8c3e762f75bed38747bea9bb1cbe0ffeffbe7700af3e512f573e7470653fe7f7e43ef956763e1db955befd583c3f5dbe9a3e60d9f0be3d6e0bbf4c3c64be835b0e3e0f5e48bfa46ba5bbab772cbccc3b1b3ea4bdac3bef05bfbc8321943e15c519be6469053e904759be4e23943e4c5b39bed32e313f944d5b3e00a6633dec36ae3ea55481bef3d64fbc32fcd43da873c63eae6cab3d353e33be540861bd46c48d3d55d314be0ea6783d4a9e973d16b5ea3d39a7183f626c153e6af206bd0936083d8d1285bed3d6a5be4680a33e7bd49ebe556b21bfdbd6dcbcdaf6ca3eaa67963dce59473eb16646bedda05f3e9f70e73df6e0ff3ee5a938be00f64f3d4cf7aebdb9b0933e948bcf3e5b92d03e55a6cd3e8bf80abf48cb0bbef06100bea67022bf5eafcb3e6a42a43e252c14bda1a56cbeaacae5be7d4fb43e01c4393e978535bf1d19b0beec8105be399b2cbfcd76aa3ce75e28bc0fb54a3e9ab7d83e6b0f9cbef4ffa13dd1b9683f46cfcbbaab80abbe4ad820bf2c6c8c3d49df17bebb39a83ea51a343eda38fabec8378b3e5f9969be35ce19bd3791753efd4d623eca74213f1f6ce4bcf5a34fbede28043f999f053e94b651be9b4346be4d80063f5e3e853e7fa9e7bdadeb493f8348c3bebf538dbd8ec058bf6e76a33ebf033dbe2e614cbebb86173f0ef9423eef1ee7bde3d4e33e311e7b3c98f893be4d57213fd3c8b4bdba25da3e7c0403bf37978e3d8711a23ea02083bff5b0d9be15450ebf298ed23d3d4b06bfbbdd443d8c1e31be8532a63e5ba71dbfc582f73dc95ec53e1db7f33e80a19a3e4cfbc83d13cd6f3f452c083e5eb440bf035b043f40f150bf719a7c3fc1e53f3e9fbf31bf31afb1bdf70a47bd24d1073ff226b53e5a327c3ee251683e5a0f7b3e254f123ff1296dbe04fa303f3f1d703b87bfdbbd1d9699bea97b2ebf371522be8d87bfbd7834d4be8641bdbe7beead3e54638dbf243c01be1d8eafbd6db37abe091dd73d0939a1bc47c02ebf8a9b193eea2b9e3c1a20cebef75c933e04dbf1beb0cf3f3ee012473f5ad2c3bd1656043f5921213f1158953d7545983e4b14573dbc330abe18da593f68a7e6bc56694ebf4c5aa2bd8593c2bda258793d01203ebe7068133e06b3a53e2ad213be966987bdfdf3793ed8224f3db9c4b23d1cdd013ffc7a6b3e352e17bef60e543f61052fbd0475edbd2be9b83e78d7d8be9c5a0d3ff7a1293d51eee4bd1794183d8046b1be71e9b43ed3608dbeda42ba3e317644beada3fbbe5c119bbe856e323cb4cf66bed4d0a3bdaffe283f8d092a3d0953893c64f122beac22cd3ec17684bd05379ebf695e253e753bbc3d5f03f8be6638293f69e7483edb943d3f763c063f3dc1a13cfef5553fee4920bf7bb5803e3ef74dbf055feabdb9fb52be64bc3cbe8740a3bdefb2143fb01610bf2a4d0f3f3554cfbed36f3ebc44dd24bfc823c1be9601083fc9f000bf97f1ab3e571bc7be7834863b171b4ebe5143823f42de88be51e39cbd9ca003bfbaafbdbde340d73ba3b9bc3dcf7f663f4792223f46d6d4bdbd724abf4b93693f4cabc1ba28621abd48a0f43d235c04beba53653f51beb8bcfca7b23e6a21013e3a8c123f65805d3d84f20cbdc2a2903e5132a13eb447e83d692193beb1c6febd98b510bd9aae1dbf2dd765bd2ce51f3d2a3d32bfdb52e5be0cd848bdb7631cbe6a27d63951299d3edf6b58be42ef14be30f8dd3d20e8983e599624befde7d5bcd85306bf2ab1593d59dc4a3e2d8f843d5982f8bd6d0d2d3e6e6f13bda3e3e23dba408e3ef428e8bd7207fabddcc5703e0d44febd8a74193fb9dccbbefdac89bd1c8668be7b93823d6376783d0e56333dc684b43e969bc13ce9c78b3eabafd93d54504ebedf11983d2621d63e200ac9bedf7a263fa94c063ef4f0973ef35143bfce6f35beb9b51fbfd1a08d3bb22b913eacdb61beb2c0afbe8cd4b3be36b3843e654b60be907ed8bd84dfa43ee47856be1dc4b03e4584153e68aca2bd2fd4fa3e8a801bbe4b69703d104e973ed5d8f53dc8b913bff38a87be0c01943f1b8212be"),dtype="<f4").astype(float).reshape((16, 32))
    _CAL_BB=np.frombuffer(bytes.fromhex("e4c03c3ea0a2283e0fe7b53d6e4566bd7b05a5be8b4b43bcf2721a3c77db183db022803eac8f013e4ab52abee895953dc374613dd0a312bfcffbdc3dd7cbecbd"),dtype="<f4").astype(float).reshape((16,))
    _CAL_C=np.frombuffer(bytes.fromhex("7360083fe82bf4bd72b12e3fa63e3abe0cd68cbe10d80e3fa4de2b3fdb0d75be3287eb3ec7ce073fbf31483f2715ca3b856148bf51c27a3f7ed0a83ed8bcd13eb4210f3f285daa3c2792023fdf1d67bec508f4be446aaa3e8c7fff3ec9b5dbbe6cdcf93e50181f3fff6f183faf103e3e4b5451bf973b603f7142733eb5a2463eb21cda3ed1486d3ee150023f55ff05be7a87d3be7adeac3e5f1c123f4966f6be19d9f73e03f51c3f3eecc93eafd1c43e996c3dbff0453e3fae51dc3e3a7bfb3dc34ea03e8d09863e1cf3f53e4f2f3ebe67c606bf29d2a73e04aaa53ed488d7be45f0103fbbba0d3f55e4613eeb7da83eef890bbf35e9353f5d4a6d3e39c18fbc6e769c3e0ca2813e0d142b3e75dc52be6cbf01bfe509db3d46c99f3e3a26b4be92a9cf3e52102b3fa43dbf3efabbfa3e6c5023bfebc7153f52c3833e19320abeb89f2d3eb023c43e37ccf93ddd7722be003e07bf436c063d56122e3e4ca755be5d6ade3e0fbadb3eb415933eda5b153f7d4fa3be65dcff3ecf0d983d8f18f7bdd1bd373e0c4ea83ec6b20f3eb0684fbe6d87dfbe69249d3de384253eec8e79be95c0b63e62b5bf3ecd9f1e3d964aff3e74d0b0beb81b153f25e5483e1fe43dbebcdc853effc1933e47c4a3bceba852beb6ec92bec7b2dd3d9629233e1a2a80becac9353e5d49e23dc430c53deb6fac3e72342cbefe75903e33d3453ccb2f73bef12da43d0650cc3e623649be809109be8480b6be2ac1c8bd6d8db23bb212b6bda4a28a3d2406e53c8a716b3e94d8b83ee3683ebd3b100e3e215e22bdb57baabe731a333ec4326a3e8945a9be18458cbe80d8edbd898249bdf6b01dbe511a9fbd548f993d1a6fec3b74cd9a3eaa00703e20106fbdf9c273bda20348be5b3b63be262e1c3c686e503e099d60befd95a3bdaac687bdbf4160bd35e10abc9d2952bd0d880a3c3924a1bdb76411be3925563d6a97443db70a2abe7b55d5bdb8f5f6be21f99ebd5589863e5b24b7bec6b4c1bdea8586bdd73393bda0e612be196d763d4f4936be3bf6a0bec5e7a7bbce94ee3d80dd963e80ac37bd070f18be8bb79ebefadc2dbe91a1373e71be09bf44a7d8bdad443f3e099550be3b3823beb7dc993e7102d4be36f4ccbecd9b65bef7e7243d896dad3e31689bbe3be395beb36014bffb0b60be38f72e3d968682be161d893d6455453ebfdd13bef4ad9abefd05863e3d2350be844afebee5ee8bbe254abcbd2038d83e7b67bbbeff85afbe3486c6be864568beba2d993d2bc7b5beedd59f3dd59dec3ea89f41be61aa97be1a42803e69f1b0beede31fbff54cb6be3f6948be5663073f2cc2fabede28a8be660facbebac7aabec9f1b13df247cebe8d52903e342c0a3f81609fbe91bd44bea7bac73ea77ef3bed89f0cbf5cba7fbef02d3bbeb2a6f93ee4e81bbffdc189be28c384be275ed9be347f86bd6060efbea10eb13e7599373f37aa94bef8ce87be7c63da3e72170ebf314517bf37c904bf92db9ebe5d10133fe21c38bfb4e00bbff8209bbe463cf5beca9a66bed658dbbe77d21b3f5fe8543f48aeadbe53859fbe6d5ac93e4b0f06bfc0630abf478b98be326516bf4485243ffdc74ebfb98e5ebe2f7ebfbd6f1011bf8093aebea124d3be9d20643fcc518a3f509fb0be815ab5be40927a3ef2093fbf8c1f07bf0e7b14bd273d22bfbfc4313f1a7731bfccc6a1be9ace1d3e"),dtype="<f4").astype(float).reshape((19, 16))
    _CAL_CB=np.frombuffer(bytes.fromhex("3727fdbc526f1f3dede0183c862817bc4cdba43d6518773c5e1a10bd2053023d1fe40e3dd54978bdf49ca73b894b463c7e3412bc2750fe3c548122bd050c8fb998b493bdb411a5bdec18debd"),dtype="<f4").astype(float).reshape((19,))
    _CAL_ARAW=_CAL_A/_CAL_SD
    _CAL_ABRAW=_CAL_AB-_CAL_ARAW@_CAL_MU
    _CAL_RAWLO=np.maximum(-20.,_CAL_MU-10.*_CAL_SD)
    _CAL_RAWHI=np.minimum(20.,_CAL_MU+10.*_CAL_SD)
